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
    assert set(decls) == {"claude", "codex", "agy", "hermes"}
    # claude: caller issues session id (A-1)
    assert decls["claude"].decl["session"]["id_origin"] == "caller"
    # codex: callee issues (A-1)
    assert decls["codex"].decl["session"]["id_origin"] == "callee"


def test_claude_command_shape() -> None:
    d = load_vendors("harness/config")["claude"]
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="D:/wt", role="review")
    assert cmd[0] == "claude"
    # A-7: claude has no `structured` key (rejects --json-schema), so NO schema flag
    assert "--json-schema" not in cmd
    # --output-format json is required to get the envelope (A-7)
    assert "--output-format" in cmd and "json" in cmd
    assert "--resume" in cmd and "S1" in cmd
    # A-6: read-only is allowedTools, execution is NOT blocked by it
    assert "--allowedTools" in cmd
    # no duplicate flags
    assert cmd.count("--resume") == 1


def test_codex_command_shape() -> None:
    d = load_vendors("harness/config")["codex"]
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="D:/wt", role="review")
    assert cmd[0].endswith("node.exe")
    assert "codex.js" in cmd[1]
    assert cmd[2] == "exec"
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
    # review role: readonly `--mode plan` applied; worktree path is injected via
    # headless `--add-dir {worktree}` (implementer needs it too, reviewer sees it).
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="D:/wt", role="review")
    assert cmd[0] == "agy"
    assert "--mode" in cmd and "plan" in cmd
    assert "--add-dir" in cmd and "D:/wt" in cmd
    # --mode plan appears once (from permission), --add-dir once (from headless)
    assert cmd.count("--mode") == 1
    assert cmd.count("--add-dir") == 1


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
    # NOTE: codex v0.147.0 で gpt-5.6-luna が実測で通る（25倍安価）。
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



def test_extract_claude_envelope_result_field() -> None:
    """Claude --output-format json returns an envelope; the answer lives in the
    `result` string which itself contains JSON. extract_result must unwrap it."""
    envelope = (
        '{"is_error": false, "type": "result", '
        '"result": "Here is the verdict: {\\"verdict\\": \\"pass\\", \\"why\\": \\"ok\\"}", '
        '"session_id": "abc"}'
    )
    assert extract_result(envelope, "result") == {"verdict": "pass", "why": "ok"}


def test_extract_agy_envelope_structured_output() -> None:
    """agy --output-format json returns an envelope with a `structured_output`
    dict already parsed. result_path should return that dict directly."""
    envelope = (
        '{"status": "SUCCESS", '
        '"structured_output": {"verdict": "pass", "why": "ok"}, '
        '"response": "the json was returned above"}'
    )
    assert extract_result(envelope, "structured_output") == {"verdict": "pass", "why": "ok"}

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
        test_hermes_no_structured_flag_and_result_extraction,
        test_extract_recovers_fenced_json,
    ]:
        fn()
        print("PASS", fn.__name__)

def test_hermes_no_structured_flag_and_result_extraction() -> None:
    """Hermes has no `structured` key; build_command must skip schema flags,
    and extract_result must return the whole object (no result_path)."""
    decls = load_vendors("harness/config")
    h = decls["hermes"]
    # schema present but vendor has no `structured` -> no schema flag emitted
    cmd = build_command(h, "do it", schema={"type": "object"}, model="tencent/hy3:free", effort="high")
    assert "--json-schema" not in cmd and "--output-schema" not in cmd
    assert "-m" in cmd and "tencent/hy3:free" in cmd
    assert "--reasoning" in cmd and "high" in cmd
    assert h.result_path() == ""
    # extract_result returns whole object when result_path is empty
    out = "session_id: 20260807_xyz\n" '{"verdict": "pass", "why": "ok"}\n'
    assert extract_result(out, "") == {"verdict": "pass", "why": "ok"}


def test_extract_recovers_fenced_json() -> None:
    """Models (e.g. hermes) often wrap JSON in a ```json ... ``` fence.
    extract_result must recover it instead of returning None."""
    fenced = (
        "session_id: 20260807_abc\n"
        "Here is the result:\n"
        "```json\n"
        '{"verdict": "pass", "score": 9}\n'
        "```\n"
    )
    assert extract_result(fenced, "") == {"verdict": "pass", "score": 9}

    # plain ``` fence (no language tag) also works
    fenced2 = "text\n```\n" '{"a": 1}\n' "```\n"
    assert extract_result(fenced2, "") == {"a": 1}

    # no JSON anywhere -> None (not a crash)
    assert extract_result("just text, no json", "") is None


def test_resolve_role_channels_list_and_dict() -> None:
    # Stage B parallel (b): roles.implement がリストならチャンネル数 = リスト長。
    # 既定の roles.implement（vendors.yaml）がリストであれば、その長さがチャンネル数。
    from harness.core.invoke import resolve_role_channels

    ch = resolve_role_channels("implement", "harness/config")
    assert isinstance(ch, list)
    assert len(ch) >= 1          # 既定は1以上のチャンネルリスト
    assert ch[0]["vendor"] == "agy"  # 既定リストの先頭チャンネルは agy

    override = [
        {"vendor": "agy", "model": "gemini-3.6-flash", "effort": "high"},
        {"vendor": "hermes", "model": "hy3:Free", "effort": "high"},
        {"vendor": "hermes", "model": "hy3:Free", "effort": "high"},
        {"vendor": "hermes", "model": "hy3:Free", "effort": "high"},
        {"vendor": "agy", "model": "gemini-3.6-pro", "effort": "high"},
    ]
    ch = resolve_role_channels("implement", "harness/config", explicit_override=override)
    assert len(ch) == 5
    assert ch[0]["vendor"] == "agy" and ch[0]["model"] == "gemini-3.6-flash"
    assert ch[1]["vendor"] == "hermes"
    assert ch[4]["vendor"] == "agy" and ch[4]["model"] == "gemini-3.6-pro"


def test_parse_channel_override() -> None:
    from harness.core.invoke import parse_channel_override
    import pytest

    ch = parse_channel_override("agy:2,hermes:3")
    assert len(ch) == 5
    assert [c["vendor"] for c in ch] == ["agy", "agy", "hermes", "hermes", "hermes"]
    ch = parse_channel_override("claude")
    assert len(ch) == 1 and ch[0]["vendor"] == "claude"
    with pytest.raises(ValueError):
        parse_channel_override("agy:notanumber")
    with pytest.raises(ValueError):
        parse_channel_override("")
