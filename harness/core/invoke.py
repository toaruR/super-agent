#!/usr/bin/env python
"""Vendor invocation adapter (ARCHITECTURE §4).

Turns a vendor declaration (config/vendors.yaml) into concrete CLI commands,
handles structured-output extraction (A-3/A-5), session resume (A-1/A-2), and
permission flags (A-6). No shell interpolation of model output anywhere.

Two modes:
- real: actually runs the subprocess (for live use)
- dry-run: returns the command list only (for tests / inspection)
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml


def git_executable() -> str:
    """Return absolute path to git binary via shutil.which('git'), falling back to 'git'."""
    return shutil.which("git") or "git"

# Known-bad model-name aliases (CODE-side, intentionally NOT in vendors.yaml).
#
# vendors.yaml may carry a friendly/shorthand model name that our gateway /
# OpenRouter rejects at call time (e.g. `hy3:Free` -> `HTTP 404: Model 'hy3:Free'
# not found`). The yaml is hand-edited by the user and must not be rewritten, so
# we normalize here instead: the yaml keeps its `hy3:Free`, but the CLI argv that
# actually reaches `hermes chat -m` uses the canonical id `tencent/hy3:free`.
#
# Only explicit, known-wrong names are remapped — anything not listed passes
# through untouched, so a legitimately different model is never silently changed.
MODEL_ALIASES = {
    "hy3:Free": "tencent/hy3:free",
    "hy3": "tencent/hy3:free",
}

# Default wall-clock timeout (seconds) for a single vendor subprocess call.
# Was 300s; raised because a killed subprocess loses all its work with no
# partial result, and a design/architect call with --allowedTools Read/Grep/Glob
# can legitimately spend minutes on internal tool-use turns exploring a large
# repo (not the harness retrying — invoke() calls the vendor once per attempt).
# Override per-role via vendors.yaml `roles.<role>.timeout` (see resolve_role()).
DEFAULT_TIMEOUT = 1800

# Idle-timeout (seconds): killed if a vendor produces no activity for this
# long. This is the *primary* stall signal (docs/design/
# timeout-liveness-watchdog.md); `timeout` above is the absolute wall-clock
# backstop. For claude/agy/codex (see _STREAM_PARSERS) activity means a
# parsed NDJSON stdout line; for hermes (no streaming stdout at all) it means
# a line from the `hermes logs -f --session <id>` tail process (see
# _run_hermes()). Any vendor not covered by either path gets no idle-timeout
# (see invoke()'s idle_timeout resolution).
DEFAULT_IDLE_TIMEOUT = 300


def normalize_model(model: str | None) -> str | None:
    """Map a known-bad model name to its canonical id; pass others through."""
    if model is None:
        return None
    return MODEL_ALIASES.get(model, model)


class VendorDecl:
    def __init__(self, name: str, decl: dict[str, Any]) -> None:
        self.name = name
        self.decl = decl

    @property
    def default_model(self) -> str | None:
        return self.decl.get("model")

    @property
    def prompt_stdin(self) -> bool:
        # When true, the prompt is passed via stdin (subprocess input=) instead
        # of as a CLI argument. Used by codex exec, which otherwise waits on
        # stdin for "additional input" when given a long prompt + --output-schema.
        return bool(self.decl.get("prompt_stdin", False))

    @property
    def model_flag(self) -> str | None:
        return self.decl.get("model_flag")

    @property
    def effort_style(self) -> str:
        # "flag"  -> `--effort <lvl>` (claude, agy)
        # "config" -> `-c <key>=<lvl>` (codex: model_reasoning_effort)
        # "model_suffix" -> append to model name (agy alt: gemini-3.6-flash-high)
        return self.decl.get("effort_style", "flag")

    @property
    def effort_flag(self) -> str | None:
        return self.decl.get("effort_flag")

    @property
    def effort_key(self) -> str | None:
        return self.decl.get("effort_key")

    def role_model(self, role: str | None) -> str | None:
        roles = self.decl.get("roles") or {}
        if role and role in roles and "model" in roles[role]:
            return roles[role]["model"]
        return None

    def role_effort(self, role: str | None) -> str | None:
        roles = self.decl.get("roles") or {}
        if role and role in roles and "effort" in roles[role]:
            return roles[role]["effort"]
        return None

    def effort_args(self, effort: str | None) -> list[str]:
        """Render effort as CLI args per the vendor's effort_style."""
        if not effort:
            return []
        style = self.effort_style
        if style == "config":
            key = self.effort_key or "model_reasoning_effort"
            return ["-c", f"{key}={effort}"]
        # default: flag style (--effort <lvl>)
        flag = self.effort_flag or "--effort"
        return [flag, effort]

    def model_with_effort(self, model: str | None, effort: str | None) -> str | None:
        """For effort_style=='model_suffix', fold effort into the model name."""
        if not model or self.effort_style != "model_suffix" or not effort:
            return model
        # e.g. gemini-3.6-flash + high -> gemini-3.6-flash-high
        return f"{model}-{effort}"

    def headless(self, prompt: str, worktree: str | None = None) -> list[str]:
        out = []
        for a in self.decl["headless"]:
            a = a.replace("{prompt}", prompt)
            if worktree is not None:
                a = a.replace("{worktree}", worktree)
            out.append(a)
        return out

    def structured_flags(self, schema: dict | None) -> list[str]:
        """Return flags to request structured output.

        claude expects the schema inline (A-5); codex expects a file path (A-5).
        Vendors without a `structured` declaration (e.g. hermes) emit no flags —
        the caller is expected to instruct JSON-only in the prompt, and
        extract_result() recovers the last JSON line from stdout.
        """
        if schema is None:
            return []
        if "structured" not in self.decl:
            return []
        flag = self.decl["structured"]["flag"]
        form = self.decl["structured"]["form"]
        if form == "inline":
            return [flag, json.dumps(schema, ensure_ascii=False)]
        # form == file
        path = self._write_schema(schema)
        return [flag, path]

    def _schema_file(self) -> Path:
        d = Path(__file__).resolve().parent.parent / "config"
        d.mkdir(exist_ok=True)
        return d / f"_schema_{self.name}.json"

    def _write_schema(self, schema: dict) -> str:
        p = self._schema_file()
        p.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def resume_flags(self, session_id: str) -> list[str]:
        base = list(self.decl["session"]["resume_flag"])
        cmd = [*base, session_id]
        extra = self.decl["session"].get("resume_extra")
        if extra:
            cmd += extra
        return cmd

    def permission_flags(self, worktree: str | None = None) -> list[str]:
        ro = self.decl["permission"]["readonly"]
        out = []
        for tok in ro:
            if tok == "{worktree}" and worktree is not None:
                out.append(worktree)
            else:
                out.append(tok)
        return out

    def result_path(self) -> str:
        # Empty string => no dotted path; extract_result returns the whole object.
        return self.decl.get("result_path", "")


def load_vendors(config_dir: str | Path) -> dict[str, VendorDecl]:
    path = Path(config_dir) / "vendors.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    # top-level `roles:` holds role->vendor/model/effort defaults, not a vendor
    vendors = {k: v for k, v in data.items() if k != "roles"}
    return {name: VendorDecl(name, decl) for name, decl in vendors.items()}


def load_role_defaults(config_dir: str | Path) -> dict[str, dict]:
    """Top-level `roles:` mapping: role -> {vendor, model, effort}.

    Single source of truth for which vendor/model/effort each pipeline stage uses
    when --vendor/--model/--effort are not given on the CLI.
    """
    path = Path(config_dir) / "vendors.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("roles", {}) or {}


DEFAULT_PATHS = {
    "design_dir": "docs/design",
    "tasks_dir": "docs/tasks",
}


def load_path_defaults(config_dir: str | Path) -> dict:
    """`design_dir` / `tasks_dir` defaults from `paths.yaml`.

    `design_dir` is where `architect`/`plan`/`drive` write design.md-style
    docs when --design_file is omitted (still `unique_path()` slug + suffix).

    `tasks_dir` is NOT where `plan`/`drive` write task files anymore — task
    files now live next to their design file, under `<design_stem>_tasks/`
    (see `tasks_dir_for_design()` / `default_task_path()`). `tasks_dir` is
    kept only as (a) the fallback directory `latest_task_file()` also scans
    for legacy flat task files, and (b) the directory read-only consumer
    commands (`implement`/`integrate`/`review-task`) would use if a design
    was never involved. Falls back to DEFAULT_PATHS if paths.yaml is missing
    or a key is absent.
    """
    path = Path(config_dir) / "paths.yaml"
    data = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    return {**DEFAULT_PATHS, **data}


def slugify(text: str, maxlen: int = 40) -> str:
    """Turn free text into a filesystem-safe stem (used to auto-name design/
    tasks files). Non-word runs become '-'; falls back to 'untitled' if the
    result would be empty (e.g. requirement text is all punctuation/empty)."""
    slug = re.sub(r"[^\w]+", "-", text.strip(), flags=re.UNICODE).strip("-")
    return (slug[:maxlen].rstrip("-")) or "untitled"


def unique_path(dir_path: str | Path, stem: str, suffix: str = ".md") -> Path:
    """Pick a non-colliding `<dir_path>/<stem><suffix>` path, appending
    `-2`, `-3`, ... if the plain stem is already taken. Pure computation —
    does not create the directory or file (callers mkdir+write themselves),
    so this stays side-effect-free under --dry-run."""
    dir_path = Path(dir_path)
    candidate = dir_path / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = dir_path / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def latest_file(dir_path: str | Path, pattern: str = "*.md") -> Path | None:
    """Most recently written file matching `pattern` in `dir_path`, or None
    if the directory is missing/empty. Used as the --task_file fallback for
    read-only consumer commands (implement/integrate/review-task) when the
    caller doesn't pass --task_file explicitly."""
    dir_path = Path(dir_path)
    if not dir_path.is_dir():
        return None
    candidates = list(dir_path.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def tasks_dir_for_design(design_path: str | Path) -> Path:
    """The task-file folder for a given design file: `<design>_tasks/`,
    a sibling of the design file itself (not under paths.yaml's tasks_dir).
    Pure computation — does not create the directory."""
    d = Path(design_path)
    return d.parent / f"{d.stem}_tasks"


def default_task_path(design_path: str, stem: str) -> Path:
    """The default (auto-named) task-file path for a given design file +
    slug stem: `<design>_tasks/<stem>.md`. Pure computation — does not create
    the directory or check for collisions (callers must run guard A: if this
    path already exists, that's an error, not a signal to pick `-2`, `-3`...
    like `unique_path()` does for design files)."""
    return tasks_dir_for_design(design_path) / f"{stem}.md"


def latest_task_file(design_dir: str | Path, fallback_dir: str | Path) -> Path | None:
    """Most recently written task file, searching both the new layout
    (`<design_dir>/*_tasks/*.md`) and the legacy flat layout (`<fallback_dir>
    /*.md`, paths.yaml's tasks_dir). Used as the --task_file fallback for
    read-only consumer commands (implement/integrate/review-task) when the
    caller doesn't pass --task_file explicitly. Returns None if neither yields
    anything; missing directories are treated as empty (not an error)."""
    design_dir = Path(design_dir)
    fallback_dir = Path(fallback_dir)
    candidates: list[Path] = []
    if design_dir.is_dir():
        candidates += list(design_dir.glob("*_tasks/*.md"))
    if fallback_dir.is_dir():
        candidates += list(fallback_dir.glob("*.md"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_role(role: str, config_dir: str | Path,
                 explicit_vendor: str | None = None,
                 explicit_model: str | None = None,
                 explicit_effort: str | None = None,
                 explicit_timeout: int | None = None) -> dict:
    """Resolve the effective vendor/model/effort/timeout for a pipeline role.

    Precedence: explicit CLI flag > role default (from vendors.yaml `roles:`)
    > invoke()'s own DEFAULT_TIMEOUT (when neither specifies a timeout, this
    returns timeout=None and callers should let invoke() apply its default).
    Returns {vendor, model, effort, timeout}.

    For the `implement` role (which may declare multiple channels as a list),
    this returns the *first* channel's vendor/model/effort/timeout as the
    default. Use `resolve_role_channels()` to get the full fan-out list.
    """
    raw = load_role_defaults(config_dir).get(role, {})
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    defaults = raw if isinstance(raw, dict) else {}
    return {
        "vendor": explicit_vendor or defaults.get("vendor"),
        "model": explicit_model or defaults.get("model"),
        "effort": explicit_effort or defaults.get("effort"),
        "timeout": explicit_timeout or defaults.get("timeout"),
    }


def parse_channel_override(spec: str) -> list[dict]:
    """Parse a CLI override like ``"agy:2,hermes:3"`` into a channel list.

    Each ``vendor:N`` contributes N channels of that vendor (model/effort left to
    the vendor/role defaults). Returns a list of {vendor, model, effort} dicts.
    Raises ValueError on a malformed spec.
    """
    channels: list[dict] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            vendor, n_s = part.split(":", 1)
            vendor = vendor.strip()
            try:
                n = int(n_s.strip())
            except ValueError:
                raise ValueError(f"invalid channel count in override: {part!r}")
            if n < 1:
                raise ValueError(f"channel count must be >= 1: {part!r}")
        else:
            vendor = part
            n = 1
        for _ in range(n):
            channels.append({"vendor": vendor, "model": None, "effort": None})
    if not channels:
        raise ValueError("empty channel override")
    return channels


def resolve_role_channels(role: str, config_dir: str | Path,
                          explicit_override: list[dict] | None = None) -> list[dict]:
    """Resolve the channel list for a pipeline role (Stage B parallel (b)).

    Each channel is {vendor, model, effort}. The number of channels equals the
    number of list elements — the user declares fan-out by listing entries:
        roles:
          implement:
            - {vendor: agy,    model: gemini-3.6-flash, effort: high}
            - {vendor: hermes, model: hy3:Free,         effort: high}
            - {vendor: hermes, model: hy3:Free,         effort: high}

    Backward compatible: a single dict `roles.implement: {vendor, model, effort}`
    is normalized to a 1-element list. An explicit override (e.g. parsed from a
    CLI flag) takes precedence over the yaml declaration.

    Returns a list of {vendor, model, effort} dicts (never empty; falls back to
    the role default vendor if the declaration is missing).
    """
    if explicit_override is not None:
        raw = explicit_override
    else:
        raw = load_role_defaults(config_dir).get(role, {})
    if isinstance(raw, dict):
        return [dict(raw)]
    if isinstance(raw, list):
        out: list[dict] = []
        for el in raw:
            if isinstance(el, str):
                out.append({"vendor": el, "model": None, "effort": None, "timeout": None})
            elif isinstance(el, dict):
                out.append({
                    "vendor": el.get("vendor"),
                    "model": el.get("model"),
                    "effort": el.get("effort"),
                    "timeout": el.get("timeout"),
                })
        if out:
            return out
    # fallback: single channel from the flat default
    d = raw if isinstance(raw, dict) else {}
    return [{
        "vendor": d.get("vendor"),
        "model": d.get("model"),
        "effort": d.get("effort"),
        "timeout": d.get("timeout"),
    }]


def build_command(
    decl: VendorDecl,
    prompt: str,
    *,
    schema: dict | None = None,
    session_id: str | None = None,
    worktree: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    role: str | None = None,
) -> list[str]:
    """Assemble the full argv for one invocation.

    Model resolution order: explicit `model` > role default > vendor default.
    Effort resolution order: explicit `effort` > role default.
    Effort rendering depends on the vendor's `effort_style` (flag / config / model_suffix).
    """
    cmd = decl.headless(prompt, worktree=worktree)
    if schema is not None:
        cmd += decl.structured_flags(schema)
    if session_id is not None:
        cmd += decl.resume_flags(session_id)
    # model/effort are resolved by the caller (cli.resolve_role) and passed in.
    # (Role defaults now live in the top-level `roles:` mapping of vendors.yaml,
    #  not per-vendor, so build_command no longer looks them up here.)
    # Normalize known-bad model names (e.g. yaml `hy3:Free` -> `tencent/hy3:free`)
    # so the live vendor call never hits a 404. Code-side alias; yaml untouched.
    m = normalize_model(model)
    eff = effort
    # model_suffix style (agy): fold effort into the model name, never emit --effort.
    # e.g. gemini-3.6-flash + high -> gemini-3.6-flash-high
    fold_effort = decl.effort_style == "model_suffix"
    if fold_effort and eff:
        m = f"{m}-{eff}"
    if m is not None and decl.model_flag:
        cmd += [decl.model_flag, m]
    # effort args for flag/config styles (model_suffix already folded above)
    if not fold_effort:
        cmd += decl.effort_args(eff)
    # Permission/readonly flags apply only to read-only roles (design/review/
    # planner: all three return structured data, never edit files).
    # Implementers must be able to write inside their worktree, so they get none
    # (isolation is via the worktree + --add-dir). Note A-6: even "readonly" flags
    # don't truly block execution for claude/codex, but agy's `--mode plan` *does*
    # hard-block edits, so we must never attach it to an implementer.
    if role in ("design", "review", "planner"):
        cmd += decl.permission_flags(worktree)
    return cmd


def _is_content_blocked(stdout: str, stderr: str) -> bool:
    """Detect a vendor content-policy block in the response.

    Hermes (Tencent Hunyuan hy3:free) occasionally refuses with a
    Chinese-language policy message and stops. Observed markers:
      - `content_policy_blocked`
      - `你好，我无法给到相关内容。` ("sorry, I can't provide that")
      - `无法给到相关内容`
    Retrying (resume the same session, or re-issue the prompt) usually
    recovers, so the caller should loop on this signal.
    """
    hay = (stdout or "") + "\n" + (stderr or "")
    markers = (
        "content_policy_blocked",
        "无法给到相关内容",
        "你好，我无法",
        "unable to provide",
    )
    return any(m in hay for m in markers)


def _extract_session_id(stdout: str) -> str | None:
    """Pull the session id a vendor prints on exit (hermes: `session_id: ...`).

    Used to resume the *same* session on a content-policy block — equivalent
    to the human "just continue" recovery that works interactively.
    """
    if not stdout:
        return None
    for ln in stdout.splitlines():
        stripped = ln.strip()
        if stripped.startswith("session_id:"):
            sid = stripped.split(":", 1)[1].strip()
            return sid or None
    return None


def extract_result(stdout: str, result_path: str) -> Any:
    """Extract the structured field from a vendor response (A-3).

    Strategy (most-to-least reliable), so all vendors recover JSON even when the
    model wraps it in a markdown fence (e.g. hermes emits ```json ... ```):
      1. last line that starts with `{` (claude/agy/codex plain JSON)
      2. content of the last ```json / ``` fenced block
      3. first balanced {...} substring anywhere in the output
    """
    lines = stdout.splitlines()

    # 1) last JSON-ish line
    candidates = [ln for ln in lines if ln.strip().startswith("{")]
    if candidates:
        obj = _parse_json_line(candidates[-1])
        if obj is not None:
            return _apply_result_path(obj, result_path)

    # 2) last fenced block (```json ... ``` or plain ``` ... ```)
    fenced = _extract_last_fence(stdout)
    if fenced is not None:
        obj = _parse_json_line(fenced)
        if obj is not None:
            return _apply_result_path(obj, result_path)

    # 3) first balanced {...} substring
    obj = _extract_first_balanced_json(stdout)
    if obj is not None:
        return _apply_result_path(obj, result_path)

    return None


def _parse_json_line(text: str) -> Any | None:
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _apply_result_path(obj: Any, result_path: str) -> Any:
    if not result_path:
        return obj
    cur: Any = obj
    for key in result_path.split("."):
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return obj  # path missing -> return whole object
            cur = cur[key]
    # claude-style envelope: result_path points at a *string* that itself holds
    # JSON (e.g. the "result" field). Recover the JSON from inside it.
    if isinstance(cur, str):
        # 1) Try last fenced block first (```json ... ``` or ```yaml ... ``` or plain ``` ... ```)
        fenced = _extract_last_fence(cur)
        if fenced is not None:
            parsed = _parse_json_line(fenced)
            if parsed is not None:
                return parsed
            try:
                import yaml
                parsed_yaml = yaml.safe_load(fenced)
                if isinstance(parsed_yaml, dict):
                    return parsed_yaml
            except Exception:
                pass
        # 2) Fall back to first balanced JSON substring anywhere in string
        recovered = _extract_first_balanced_json(cur)
        if recovered is not None:
            return recovered
    return cur


def _extract_last_fence(stdout: str) -> str | None:
    """Return the inner content of the last markdown code fence, if any."""
    import re
    # matches ```lang\n...\n``` or ```\n...\n```
    fences = re.findall(r"```[a-zA-Z0-9]*\n(.*?)\n```", stdout, flags=re.DOTALL)
    return fences[-1].strip() if fences else None


def _extract_first_balanced_json(stdout: str) -> Any | None:
    """Find the first balanced {...} (or [...] ) substring and parse it."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stdout.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(stdout)):
            if stdout[i] == opener:
                depth += 1
            elif stdout[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stdout[start : i + 1])
                    except (json.JSONDecodeError, ValueError):
                        return None
    return None


# --- Streaming (liveness) support -------------------------------------------
#
# claude/agy/codex support an NDJSON streaming output mode; the exact flags and
# shapes below are empirically measured (see CLAUDE.md "ハマりポイント" and
# docs/design/timeout-liveness-watchdog.md). hermes has no streaming mode at
# all (nothing is printed until the whole response is ready), so it is
# deliberately absent from this table — invoke() only applies idle-timeout
# stall detection to vendors listed here (see idle_timeout resolution below).
#
# Each entry provides:
#   detail(obj)      -> a short human string for progress_cb, or None to skip.
#                        Unknown type/event values still produce a generic
#                        "type=..."/"event=..." string (so future vendor-side
#                        additions don't silently stop being reported).
#   reconstruct(objs) -> for vendors whose last NDJSON line is turn metadata
#                        rather than the answer (codex), rebuild the text
#                        extract_result() should scrape, from the full list of
#                        parsed line objects. None means "use the raw joined
#                        stdout lines as-is" (claude/agy: the last NDJSON line
#                        already *is* the same envelope non-streaming mode used
#                        to produce, so extract_result's last-JSON-line
#                        heuristic keeps working unchanged).

def _claude_stream_detail(obj: dict[str, Any]) -> str | None:
    t = obj.get("type")
    return f"type={t}" if t else None


def _agy_stream_detail(obj: dict[str, Any]) -> str | None:
    ev = obj.get("event")
    if ev == "step_update":
        # payload nests under a "step_update" key (measured live, 2026-08-11):
        # {"event":"step_update","step_update":{"state":..,"step_type":..,...}}
        su = obj.get("step_update") or {}
        step_type = su.get("step_type", "")
        state = su.get("state", "")
        return f"step_update {step_type} {state}".strip()
    return f"event={ev}" if ev else None


def _codex_stream_detail(obj: dict[str, Any]) -> str | None:
    t = obj.get("type")
    return f"type={t}" if t else None


def _codex_reconstruct_text(parsed_lines: list[dict[str, Any]]) -> str | None:
    """codex --json's final NDJSON line is `turn.completed`/`turn.failed`
    (usage stats / error), not the answer (unlike claude/agy, whose terminal
    line IS the envelope carrying the answer). The actual text lives in
    `item.completed` events with `item.type=="agent_message"`. Reconstruct the
    equivalent of the old plain-text stdout from the last such item so
    extract_result()'s line/fence heuristics keep working unchanged. Returns
    None (caller falls back to the raw joined stdout) if no agent_message item
    was seen, e.g. a `turn.failed` run."""
    text = None
    for obj in parsed_lines:
        if obj.get("type") == "item.completed":
            item = obj.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                text = item["text"]
    return text


_STREAM_PARSERS: dict[str, dict[str, Any]] = {
    "claude": {"detail": _claude_stream_detail, "reconstruct": None},
    "agy": {"detail": _agy_stream_detail, "reconstruct": None},
    "codex": {"detail": _codex_stream_detail, "reconstruct": _codex_reconstruct_text},
}


def _try_parse_json_object(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _reader_thread(stream: Any, name: str,
                    q: "queue.Queue[tuple[str, str | None]]") -> None:
    """Pump lines from `stream` into `q` as (name, line); (name, None) on EOF.

    Runs in its own thread per pipe (stdout/stderr) — Windows can't select()
    on pipes, so this is how the main loop gets a single wait-with-timeout
    point across both streams (queue.Queue.get(timeout=...)).
    """
    try:
        for line in iter(stream.readline, ""):
            q.put((name, line.rstrip("\r\n")))
    finally:
        q.put((name, None))


def atomic_write_draft(draft_path: str | Path | None, content: str) -> None:
    """Safely write content to draft_path via atomic replace (.tmp + os.replace)."""
    if not draft_path or not content:
        return
    p = Path(draft_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8", errors="replace")
        os.replace(tmp, p)
    except OSError:
        pass  # best-effort; atomic flush failure should not crash execution


def _run_streaming(
    cmd: list[str],
    *,
    cwd: str | None,
    stdin_input: str | None,
    timeout: float | None,
    idle_timeout: float | None,
    progress_cb: Callable[[str], None] | None,
    vendor_name: str,
    draft_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run one vendor subprocess, killed on idle-timeout (no stdout/stderr
    activity for `idle_timeout` seconds) as well as the absolute `timeout`.

    Returns {returncode, stdout, stderr, timed_out, stall_reason}. `stdout` is
    the raw joined stdout lines, UNLESS the vendor declares a `reconstruct`
    (codex) and produced at least one parseable line, in which case it is
    replaced by the reconstructed answer text so extract_result() keeps
    working unchanged. `timed_out` is True if the process had to be killed
    (idle or absolute); `stall_reason` is "idle"/"absolute"/None.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd,
        stdin=subprocess.PIPE if stdin_input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", shell=False,
    )
    if stdin_input is not None:
        # codex (prompt_stdin): write the prompt then close, same as
        # subprocess.run(input=...) used to. Popen has no `input=` kwarg, so
        # this is done by hand. Prompt sizes here (task/design text) are far
        # below typical pipe buffer sizes, so a synchronous write is safe.
        proc.stdin.write(stdin_input)
        proc.stdin.close()
    q: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
    threads = [
        threading.Thread(target=_reader_thread, args=(proc.stdout, "stdout", q), daemon=True),
        threading.Thread(target=_reader_thread, args=(proc.stderr, "stderr", q), daemon=True),
    ]
    for t in threads:
        t.start()

    parser = _STREAM_PARSERS.get(vendor_name)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    parsed_lines: list[dict[str, Any]] = []
    open_streams = {"stdout", "stderr"}
    start = time.monotonic()
    last_activity = start
    timed_out = False
    stall_reason: str | None = None

    while open_streams:
        waits = []
        if idle_timeout is not None:
            waits.append(("idle", idle_timeout - (time.monotonic() - last_activity)))
        if timeout is not None:
            waits.append(("absolute", timeout - (time.monotonic() - start)))
        if not waits:
            wait_kind, wait = None, None
        else:
            wait_kind, wait = min(waits, key=lambda kv: kv[1])
        if wait is not None and wait <= 0:
            timed_out = True
            stall_reason = wait_kind
            break
        try:
            name, line = q.get(timeout=wait)
        except queue.Empty:
            timed_out = True
            stall_reason = wait_kind
            break
        if line is None:
            open_streams.discard(name)
            continue
        last_activity = time.monotonic()
        if name == "stdout":
            stdout_lines.append(line)
            if parser is not None:
                obj = _try_parse_json_object(line)
                if obj is not None:
                    parsed_lines.append(obj)
                    if progress_cb is not None:
                        detail = parser["detail"](obj)
                        if detail:
                            progress_cb(detail)
            if draft_path is not None:
                reconstruct = parser["reconstruct"] if parser is not None else None
                cur_text = reconstruct(parsed_lines) if reconstruct is not None else "\n".join(stdout_lines)
                if cur_text:
                    atomic_write_draft(draft_path, cur_text)
        else:
            stderr_lines.append(line)

    if timed_out:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for t in threads:
        t.join(timeout=2)

    stdout_text = "\n".join(stdout_lines)
    reconstruct = parser["reconstruct"] if parser is not None else None
    if reconstruct is not None:
        reconstructed = reconstruct(parsed_lines)
        if reconstructed is not None:
            stdout_text = reconstructed

    if draft_path is not None and stdout_text:
        atomic_write_draft(draft_path, stdout_text)

    return {
        "returncode": proc.returncode,
        "stdout": stdout_text,
        "stderr": "\n".join(stderr_lines),
        "timed_out": timed_out,
        "stall_reason": stall_reason,
    }


def _start_hermes_log_tail(
    exe: str, session_id: str, q: "queue.Queue[tuple[str, str | None]]"
) -> tuple[subprocess.Popen | None, threading.Thread | None]:
    """Spawn ``<exe> logs -f --session <id>`` and pump its stdout lines into
    `q` tagged "logtail" (docs/design/timeout-liveness-watchdog.md §2).

    hermes prints nothing to its own stdout until the whole response is
    ready, so this side process is the only liveness signal available once
    the session id is known. Best-effort: if the log-tail process fails to
    start (e.g. hermes not on PATH under a test double), returns (None, None)
    rather than raising — liveness then silently degrades to "no activity
    until the main process exits", same as before this feature existed.
    """
    try:
        log_proc = subprocess.Popen(
            [exe, "logs", "-f", "--session", session_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", shell=False,
        )
    except OSError:
        return None, None
    t = threading.Thread(target=_reader_thread, args=(log_proc.stdout, "logtail", q), daemon=True)
    t.start()
    return log_proc, t


def _run_hermes(
    cmd: list[str],
    *,
    cwd: str | None,
    timeout: float | None,
    idle_timeout: float | None,
    progress_cb: Callable[[str], None] | None,
    draft_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run hermes with log-tail-derived liveness (docs/design/
    timeout-liveness-watchdog.md §2), since hermes has no streaming NDJSON
    mode at all (nothing on stdout until the whole response is ready) except
    a `session_id: <id>` line printed first (measured live; no extra flag
    needed — see CLAUDE.md's ハマりポイント).

    Once that id is captured, a second `hermes logs -f --session <id>`
    process is spawned and its lines feed the SAME idle-timeout clock as
    stdout/stderr, so a genuinely stalled hermes call can still be killed
    (previously idle_timeout was unconditionally disabled for hermes because
    there was no liveness signal at all).

    Returns the same shape as `_run_streaming()`.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", shell=False,
    )
    q: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
    threads = [
        threading.Thread(target=_reader_thread, args=(proc.stdout, "stdout", q), daemon=True),
        threading.Thread(target=_reader_thread, args=(proc.stderr, "stderr", q), daemon=True),
    ]
    for t in threads:
        t.start()

    log_proc: subprocess.Popen | None = None
    log_thread: threading.Thread | None = None
    session_id: str | None = None

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    open_streams = {"stdout", "stderr"}
    start = time.monotonic()
    last_activity = start
    timed_out = False
    stall_reason: str | None = None

    while open_streams:
        waits = []
        if idle_timeout is not None:
            waits.append(("idle", idle_timeout - (time.monotonic() - last_activity)))
        if timeout is not None:
            waits.append(("absolute", timeout - (time.monotonic() - start)))
        if not waits:
            wait_kind, wait = None, None
        else:
            wait_kind, wait = min(waits, key=lambda kv: kv[1])
        if wait is not None and wait <= 0:
            timed_out = True
            stall_reason = wait_kind
            break
        try:
            name, line = q.get(timeout=wait)
        except queue.Empty:
            timed_out = True
            stall_reason = wait_kind
            break
        if line is None:
            open_streams.discard(name)  # logtail is never in open_streams; harmless no-op
            continue
        last_activity = time.monotonic()
        if name == "stdout":
            stdout_lines.append(line)
            if session_id is None:
                sid = _extract_session_id(line)
                if sid is not None:
                    session_id = sid
                    if progress_cb is not None:
                        progress_cb(f"session_id={sid}")
                    log_proc, log_thread = _start_hermes_log_tail(cmd[0], sid, q)
            if draft_path is not None and stdout_lines:
                atomic_write_draft(draft_path, "\n".join(stdout_lines))
        elif name == "stderr":
            stderr_lines.append(line)
        else:  # "logtail"
            if progress_cb is not None:
                progress_cb(line[:200])
            if draft_path is not None and stdout_lines:
                atomic_write_draft(draft_path, "\n".join(stdout_lines))

    if timed_out:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for t in threads:
        t.join(timeout=2)

    if log_proc is not None:
        log_proc.kill()
        try:
            log_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if log_thread is not None:
        log_thread.join(timeout=2)

    stdout_text = "\n".join(stdout_lines)
    if draft_path is not None and stdout_text:
        atomic_write_draft(draft_path, stdout_text)

    return {
        "returncode": proc.returncode,
        "stdout": stdout_text,
        "stderr": "\n".join(stderr_lines),
        "timed_out": timed_out,
        "stall_reason": stall_reason,
    }


def invoke(
    decl: VendorDecl,
    prompt: str,
    *,
    schema: dict | None = None,
    session_id: str | None = None,
    worktree: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    role: str | None = None,
    cwd: str | None = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int | None = DEFAULT_IDLE_TIMEOUT,
    progress_cb: Callable[[str], None] | None = None,
    max_retries: int = 3,
    draft_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run `decl`'s headless command for `prompt`, retrying on content-block.

    Execution is via Popen + reader threads (not subprocess.run): a stalled
    vendor is killed on `idle_timeout` seconds of no activity (the primary
    signal), with `timeout` as the absolute wall-clock backstop. For
    claude/agy/codex (see _STREAM_PARSERS) activity means a parsed NDJSON
    stdout line; for hermes (no streaming output at all) it means either
    stdout/stderr, or a line from the `hermes logs -f --session <id>`
    process tailed once the session id is known (see `_run_hermes()`,
    docs/design/timeout-liveness-watchdog.md §2). Every other vendor gets no
    idle-timeout (treated as None) — none is currently declared.

    `progress_cb(detail)`, if given, is called with a short human-readable
    string for every unit of observed activity. Callers wire this to the
    progress side-channel (harness.core.progress) themselves — invoke() has
    no task_id to key a progress file by.

    `cwd`, if given, is the working directory for the vendor subprocess
    (e.g. a task's worktree — see harness.roles.implementer).

    Raises subprocess.TimeoutExpired (matching subprocess.run's old contract)
    if an attempt is killed for stalling, with partial output/stderr attached.
    """
    cmd = build_command(decl, prompt, schema=schema, session_id=session_id,
                        worktree=worktree, model=model, effort=effort, role=role)
    if dry_run:
        return {"cmd": cmd, "dry_run": True}
    stdin_input = prompt if decl.prompt_stdin else None
    effective_idle_timeout = (
        idle_timeout if (decl.name in _STREAM_PARSERS or decl.name == "hermes") else None
    )

    last: dict[str, Any] = {}
    resume_sid: str | None = session_id
    for attempt in range(1, max_retries + 1):
        # Resume the prior session on retries if the vendor supports it
        # (hermes prints `session_id:` and accepts `--resume <id>`). This is the
        # programmatic equivalent of the interactive "just continue" recovery
        # that works around intermittent content-policy blocks.
        run_cmd = cmd
        if attempt > 1 and resume_sid is not None:
            run_cmd = build_command(
                decl, prompt, schema=schema, session_id=resume_sid,
                worktree=worktree, model=model, effort=effort, role=role,
            )
        if decl.name == "hermes":
            run = _run_hermes(
                run_cmd, cwd=cwd, timeout=timeout,
                idle_timeout=effective_idle_timeout, progress_cb=progress_cb,
                draft_path=draft_path,
            )
        else:
            run = _run_streaming(
                run_cmd, cwd=cwd, stdin_input=stdin_input, timeout=timeout,
                idle_timeout=effective_idle_timeout, progress_cb=progress_cb,
                vendor_name=decl.name, draft_path=draft_path,
            )
        if run["timed_out"]:
            effective_timeout = (
                effective_idle_timeout if run["stall_reason"] == "idle" else timeout
            )
            raise subprocess.TimeoutExpired(
                run_cmd, effective_timeout, output=run["stdout"], stderr=run["stderr"],
            )
        last = {
            "cmd": run_cmd,
            "returncode": run["returncode"],
            "stdout": run["stdout"],
            "stderr": run["stderr"],
            "result": extract_result(run["stdout"], decl.result_path()),
            "attempt": attempt,
        }
        # Content-policy block? Retry by resuming the same session (or, if no
        # session id yet, the next attempt re-issues the same prompt fresh).
        if _is_content_blocked(run["stdout"], run["stderr"]):
            sid = _extract_session_id(run["stdout"])
            if sid:
                resume_sid = sid
            if attempt < max_retries:
                continue
            # exhausted retries — return the last (blocked) response as-is
            last["content_blocked"] = True
            return last
        # Not blocked: success (or a genuine failure), stop retrying.
        return last
    return last
