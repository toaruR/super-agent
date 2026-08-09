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
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="./wt", role="review")
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
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="./wt", role="review")
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
    cmd = build_command(d, "do it", schema=SCHEMA, session_id="S1", worktree="./wt", role="review")
    assert cmd[0] == "agy"
    assert "--mode" in cmd and "plan" in cmd
    assert "--add-dir" in cmd and "./wt" in cmd
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

    # implement role -> hermes / hy3:Free / high (flag-style effort)
    ri = resolve_role("implement", "harness/config")
    assert ri["vendor"] == "hermes" and ri["model"] == "hy3:Free" and ri["effort"] == "high"
    he = load_vendors("harness/config")["hermes"]
    cmd = build_command(he, "P", model=ri["model"], effort=ri["effort"])
    assert "-m" in cmd and "tencent/hy3:free" in cmd  # hy3:Free normalized
    assert "--reasoning" in cmd and "high" in cmd

    # review role -> agy / gemini-3.6-flash / high (model_suffix folded)
    rr = resolve_role("review", "harness/config")
    assert rr["vendor"] == "agy" and rr["model"] == "gemini-3.6-flash"
    ag = load_vendors("harness/config")["agy"]
    cmd = build_command(ag, "P", model=rr["model"], effort=rr["effort"])
    assert "gemini-3.6-flash-high" in cmd  # suffixed

    # codex uses config-style effort (not tied to any current role default,
    # but still a declared vendor and its build_command path stays covered)
    cx = load_vendors("harness/config")["codex"]
    cmd = build_command(cx, "P", model="gpt-5.6-luna", effort="high")
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
    assert ch[0]["vendor"] == "hermes"  # 既定リストの先頭チャンネルは hermes

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


def test_normalize_model_aliases() -> None:
    """Known-bad yaml model names map to live ids; unlisted models pass through.

    Empirical basis: `hy3:Free` (as written in vendors.yaml) yields
    `HTTP 404: Model 'hy3:Free' not found`; the canonical OpenRouter id is
    `tencent/hy3:free`. We normalize in code so the hand-edited yaml is never
    rewritten (the user forbids touching it).
    """
    from harness.core.invoke import normalize_model

    assert normalize_model("hy3:Free") == "tencent/hy3:free"
    assert normalize_model("hy3") == "tencent/hy3:free"
    # anything not a known-bad alias is preserved verbatim (no silent rewrites)
    assert normalize_model("claude-opus-4.8") == "claude-opus-4.8"
    assert normalize_model(None) is None


def test_build_command_normalizes_model() -> None:
    """build_command must rewrite a known-bad model name in the emitted argv."""
    from harness.core.invoke import build_command

    d = load_vendors("harness/config")["hermes"]
    cmd = build_command(d, "do it", model="hy3:Free", effort="high", role="implement")
    # -m tencent/hy3:free (normalized), NOT -m hy3:Free
    i = cmd.index("-m")
    assert cmd[i + 1] == "tencent/hy3:free"


def test_is_content_blocked_markers() -> None:
    """Block detection catches the observed hermes policy messages."""
    from harness.core.invoke import _is_content_blocked

    assert _is_content_blocked("你好，我无法给到相关内容。", "") is True
    assert _is_content_blocked("", "content_policy_blocked: ...") is True
    assert _is_content_blocked("normal code output", "") is False
    assert _is_content_blocked("I'm unable to provide that", "") is True


def test_extract_session_id() -> None:
    from harness.core.invoke import _extract_session_id

    out = "some text\nsession_id: 20260808_213016_e5a356\nmore"
    assert _extract_session_id(out) == "20260808_213016_e5a356"
    assert _extract_session_id("no id here") is None


def test_invoke_retries_on_content_block(monkeypatch) -> None:
    """invoke() must retry (resuming the session) when hermes blocks, then
    succeed on a later attempt instead of returning the blocked response."""
    import harness.core.invoke as inv
    from harness.core.invoke import VendorDecl

    # Fake vendor decl that supports session resume (like hermes).
    decl = VendorDecl("hermes", {
        "model_flag": "-m", "effort_flag": "--reasoning",
        "headless": ["hermes", "chat", "-q", "{prompt}", "-Q"],
        "session": {"resume_flag": ["--resume"]},
    })

    # Simulated hermes: 1st call blocked (emits a session id), 2nd succeeds.
    calls = {"n": 0}

    class _FakeProc:
        def __init__(self, rc, out, err):
            self.returncode = rc; self.stdout = out; self.stderr = err

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # blocked, but prints a session id we can resume with
            return _FakeProc(0, "你好，我无法给到相关内容。\nsession_id: ABC123\n", "")
        # resumed attempt: success with code
        return _FakeProc(0, "```python\ndef build_model(e):\n    return {}\n```\nsession_id: ABC123\n", "")

    monkeypatch.setattr(inv.subprocess, "run", fake_run)

    res = inv.invoke(decl, "implement this", model="tencent/hy3:free",
                     effort="high", role="implement", timeout=10, max_retries=3)
    # retried exactly twice (blocked once, succeeded on 2nd)
    assert calls["n"] == 2
    assert res.get("content_blocked") is not True
    # the resume flag was used on the 2nd attempt
    assert "--resume" in res["cmd"]
    assert "ABC123" in res["cmd"]
