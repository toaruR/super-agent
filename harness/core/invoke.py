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
import subprocess
from pathlib import Path
from typing import Any

import yaml

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


def resolve_role(role: str, config_dir: str | Path,
                 explicit_vendor: str | None = None,
                 explicit_model: str | None = None,
                 explicit_effort: str | None = None) -> dict:
    """Resolve the effective vendor/model/effort for a pipeline role.

    Precedence: explicit CLI flag > role default (from vendors.yaml `roles:`).
    Returns {vendor, model, effort}.

    For the `implement` role (which may declare multiple channels as a list),
    this returns the *first* channel's vendor/model/effort as the default. Use
    `resolve_role_channels()` to get the full fan-out list.
    """
    raw = load_role_defaults(config_dir).get(role, {})
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    defaults = raw if isinstance(raw, dict) else {}
    return {
        "vendor": explicit_vendor or defaults.get("vendor"),
        "model": explicit_model or defaults.get("model"),
        "effort": explicit_effort or defaults.get("effort"),
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
                out.append({"vendor": el, "model": None, "effort": None})
            elif isinstance(el, dict):
                out.append({
                    "vendor": el.get("vendor"),
                    "model": el.get("model"),
                    "effort": el.get("effort"),
                })
        if out:
            return out
    # fallback: single channel from the flat default
    d = raw if isinstance(raw, dict) else {}
    return [{
        "vendor": d.get("vendor"),
        "model": d.get("model"),
        "effort": d.get("effort"),
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
    # Permission/readonly flags apply only to read-only roles (design/review).
    # Implementers must be able to write inside their worktree, so they get none
    # (isolation is via the worktree + --add-dir). Note A-6: even "readonly" flags
    # don't truly block execution for claude/codex, but agy's `--mode plan` *does*
    # hard-block edits, so we must never attach it to an implementer.
    if role in ("design", "review"):
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
        recovered = _extract_first_balanced_json(cur)
        if recovered is not None:
            return recovered
        fenced = _extract_last_fence(cur)
        if fenced is not None:
            parsed = _parse_json_line(fenced)
            if parsed is not None:
                return parsed
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
    dry_run: bool = False,
    timeout: int = 300,
    max_retries: int = 3,
) -> dict[str, Any]:
    cmd = build_command(decl, prompt, schema=schema, session_id=session_id,
                        worktree=worktree, model=model, effort=effort, role=role)
    if dry_run:
        return {"cmd": cmd, "dry_run": True}
    # codex (prompt_stdin) reads the prompt from stdin; others get DEVNULL so
    # they never block waiting on stdin. Passing `input=` makes subprocess use a
    # pipe for stdin automatically, so we must not also set stdin=DEVNULL.
    stdin_kw = {"input": prompt} if decl.prompt_stdin else {"stdin": subprocess.DEVNULL}

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
        proc = subprocess.run(run_cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              shell=False, timeout=timeout, **stdin_kw)
        last = {
            "cmd": run_cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "result": extract_result(proc.stdout, decl.result_path()),
            "attempt": attempt,
        }
        # Content-policy block? Retry by resuming the same session (or, if no
        # session id yet, the next attempt re-issues the same prompt fresh).
        if _is_content_blocked(proc.stdout, proc.stderr):
            sid = _extract_session_id(proc.stdout)
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
