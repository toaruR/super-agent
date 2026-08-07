#!/usr/bin/env python
"""Tests for the vendor invocation adapter (Stage A).

These tests run dry-run (command assembly only) so they don't need live
vendor CLIs. They lock in the empirically-correct flag assembly (EVIDENCE A-1..A-6).
"""
from __future__ import annotations

from harness.core.invoke import (
    build_command,
    extract_result,
    load_vendors,
)

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


def test_load_vendors() -> None:
    decls = load_vendors("harness/config")
    assert set(decls) == {"claude", "codex", "agy"}
    # claude: caller issues session id (A-1)
    assert decls["claude"].decl["session"]["id_origin"] == "caller"
    # codex: callee issues (A-1)
    assert decls["codex"].decl["session"]["id_origin"] == "callee"


def test_claude_command_shape() -> None:
    d = load_vendors("harness/config")["claude"]
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="D:/wt")
    assert cmd[0] == "claude"
    assert "--json-schema" in cmd  # inline form (A-5)
    # inline schema is a single arg (not a file path)
    js_idx = cmd.index("--json-schema")
    assert cmd[js_idx + 1].startswith("{") and "D:" not in cmd[js_idx + 1]
    assert "--resume" in cmd and "S1" in cmd
    # A-6: read-only is allowedTools, execution is NOT blocked by it
    assert "--allowedTools" in cmd
    # no duplicate flags
    assert cmd.count("--resume") == 1


def test_codex_command_shape() -> None:
    d = load_vendors("harness/config")["codex"]
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="D:/wt")
    assert cmd[:2] == ["codex", "exec"]
    assert "--output-schema" in cmd  # file form (A-5)
    # file form writes a path, not inline json
    os_idx = cmd.index("--output-schema")
    assert cmd[os_idx + 1].endswith(".json")
    assert "--resume" in cmd
    # A-2: resume re-asserts full-auto (sandbox dropped by resume, re-added by permission)
    assert "--full-auto" in cmd
    assert cmd.count("--sandbox") == 1  # not duplicated
    assert "read-only" in cmd


def test_agy_command_shape() -> None:
    d = load_vendors("harness/config")["agy"]
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="D:/wt")
    assert cmd[0] == "agy"
    assert "--mode" in cmd and "plan" in cmd
    assert "--add-dir" in cmd and "D:/wt" in cmd
    # --mode plan appears once (from permission), not doubled with headless
    assert cmd.count("--mode") == 1


def test_extract_last_json_line() -> None:
    # claude/agy emit multiple agent_message events; take the last JSON line (A-3)
    out = 'some noise\n{"role":"assistant"}\n{"structured_output":{"x":"hi"}}'
    assert extract_result(out, ".structured_output") == {"x": "hi"}


def test_extract_no_json() -> None:
    assert extract_result("plain text", ".structured_output") is None


def test_role_model_and_effort_resolution() -> None:
    # role defaults now live in the top-level `roles:` mapping of vendors.yaml,
    # resolved by resolve_role(); build_command consumes the resolved model/effort.
    from harness.core.invoke import resolve_role

    # design role -> claude / claude-sonnet-5 / high
    rd = resolve_role("design", "harness/config")
    assert rd == {"vendor": "claude", "model": "claude-sonnet-5", "effort": "high"}
    cl = load_vendors("harness/config")["claude"]
    cmd = build_command(cl, "P", model=rd["model"], effort=rd["effort"])
    assert "--model" in cmd and "claude-sonnet-5" in cmd
    assert "--effort" in cmd and "high" in cmd

    # implement role -> agy / gemini-3.6-flash / high (model_suffix folded)
    ri = resolve_role("implement", "harness/config")
    assert ri["vendor"] == "agy" and ri["model"] == "gemini-3.6-flash" and ri["effort"] == "high"
    ag = load_vendors("harness/config")["agy"]
    cmd = build_command(ag, "P", model=ri["model"], effort=ri["effort"])
    assert "gemini-3.6-flash-high" in cmd  # suffixed

    # review role -> codex / gpt-5.6-luna / high (config-style effort)
    rr = resolve_role("review", "harness/config")
    assert rr["vendor"] == "codex" and rr["model"] == "gpt-5.6-luna"
    cx = load_vendors("harness/config")["codex"]
    cmd = build_command(cx, "P", model=rr["model"], effort=rr["effort"])
    assert "-m" in cmd and "gpt-5.6-luna" in cmd
    ci = cmd.index("-c")
    assert cmd[ci + 1] == "model_reasoning_effort=high"

    # explicit CLI override wins over role default
    ro = resolve_role("design", "harness/config", explicit_vendor="agy",
                     explicit_model="custom-m", explicit_effort="low")
    assert ro == {"vendor": "agy", "model": "custom-m", "effort": "low"}

    # explicit --model on agy is suffixed (model_suffix always folds effort in)
    cmd = build_command(ag, "P", model="other-model", effort="low")
    assert "other-model-low" in cmd and "--effort" not in cmd


if __name__ == "__main__":
    import sys

    for fn in [
        test_load_vendors,
        test_claude_command_shape,
        test_codex_command_shape,
        test_agy_command_shape,
        test_extract_last_json_line,
        test_extract_no_json,
        test_role_model_and_effort_resolution,
    ]:
        fn()
        print("PASS", fn.__name__)
