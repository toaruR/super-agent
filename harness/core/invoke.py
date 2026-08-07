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


class VendorDecl:
    def __init__(self, name: str, decl: dict[str, Any]) -> None:
        self.name = name
        self.decl = decl

    @property
    def default_model(self) -> str | None:
        return self.decl.get("model")

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

    def headless(self, prompt: str) -> list[str]:
        return [a.replace("{prompt}", prompt) for a in self.decl["headless"]]

    def structured_flags(self, schema: dict | None) -> list[str]:
        """Return flags to request structured output.

        claude expects the schema inline (A-5); codex expects a file path (A-5).
        """
        if schema is None:
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
        return self.decl.get("result_path", ".structured_output")


def load_vendors(config_dir: str | Path) -> dict[str, VendorDecl]:
    path = Path(config_dir) / "vendors.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return {name: VendorDecl(name, decl) for name, decl in data.items()}


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
    cmd = decl.headless(prompt)
    if schema is not None:
        cmd += decl.structured_flags(schema)
    if session_id is not None:
        cmd += decl.resume_flags(session_id)
    # model: explicit > role default > vendor default; only emit if a flag is declared
    m = model or decl.role_model(role) or decl.default_model
    eff = effort or decl.role_effort(role)
    # effort folded into model name only when using the role-default model
    # (explicit --model overrides should not be suffixed)
    fold_effort = (model is None) and decl.effort_style == "model_suffix"
    if fold_effort:
        m = decl.model_with_effort(m, eff)
    if m is not None and decl.model_flag:
        cmd += [decl.model_flag, m]
    # effort args for flag/config styles (model_suffix already folded above)
    if not fold_effort:
        cmd += decl.effort_args(eff)
    cmd += decl.permission_flags(worktree)
    return cmd


def extract_result(stdout: str, result_path: str) -> Any:
    """Extract the structured field from a vendor response (A-3).

    claude/agy: a JSON object (possibly one of several agent_message events);
    take the last non-empty JSON line. codex: a JSON object with the schema keys.
    """
    candidates = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
    if not candidates:
        return None
    obj = json.loads(candidates[-1])
    # support a dotted result_path (e.g. ".structured_output")
    cur: Any = obj
    for key in result_path.split("."):
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return obj  # path missing -> return whole object
            cur = cur[key]
    return cur


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
) -> dict[str, Any]:
    cmd = build_command(decl, prompt, schema=schema, session_id=session_id,
                        worktree=worktree, model=model, effort=effort, role=role)
    if dry_run:
        return {"cmd": cmd, "dry_run": True}
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          shell=False, timeout=timeout)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "result": extract_result(proc.stdout, decl.result_path()),
    }
