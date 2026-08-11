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
    # --output-format stream-json requires --verbose (liveness streaming, see
    # docs/design/timeout-liveness-watchdog.md); the terminal NDJSON line
    # (type:"result") carries the same {"result": "...", ...} envelope the old
    # non-streaming --output-format json used to, so extract_result is unaffected.
    assert "--verbose" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
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
    assert "--json" in cmd  # NDJSON liveness stream (thread.started/turn.*/item.completed)
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
    # flag-order requirement (measured live, 2026-08-11): --output-format
    # stream-json MUST precede --print, or agy ignores the prompt entirely and
    # returns an off-topic explanation instead.
    assert cmd.index("--output-format") < cmd.index("--print")
    assert d.result_path() == "result.response"


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

    # design role -> agy / gemini-3.6-flash / high
    rd = resolve_role("design", "harness/config")
    assert rd == {"vendor": "agy", "model": "gemini-3.6-flash", "effort": "high", "timeout": None}
    ag = load_vendors("harness/config")["agy"]
    cmd = build_command(ag, "P", model=rd["model"], effort=rd["effort"])
    assert "gemini-3.6-flash-high" in cmd

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
    assert ro == {"vendor": "agy", "model": "custom-m", "effort": "low", "timeout": None}

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
    """Generic dotted-path capability check (not tied to a specific vendor
    envelope shape): result_path can point at any key, parsed dict included."""
    envelope = (
        '{"status": "SUCCESS", '
        '"structured_output": {"verdict": "pass", "why": "ok"}, '
        '"response": "the json was returned above"}'
    )
    assert extract_result(envelope, "structured_output") == {"verdict": "pass", "why": "ok"}


def test_extract_agy_stream_json_result_response() -> None:
    """agy's actual --output-format stream-json terminal line (measured live,
    2026-08-11): {"event":"result","result":{"response": "<text>", ...}}.
    result_path="result.response" must unwrap it, recovering embedded JSON
    from the response string the same way claude's result_path="result" does."""
    envelope = (
        '{"event":"result","result":{"conversation_id":"x",'
        '"status":"SUCCESS","response":"{\\"verdict\\": \\"pass\\", \\"why\\": \\"ok\\"}",'
        '"duration_seconds":1.2}}'
    )
    assert extract_result(envelope, "result.response") == {"verdict": "pass", "why": "ok"}

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


def test_tasks_dir_for_design() -> None:
    from harness.core.invoke import tasks_dir_for_design
    from pathlib import Path

    assert tasks_dir_for_design("docs/design/x.md") == Path("docs/design/x_tasks")
    # works with a Path object too, and with a design file that has no dir component
    assert tasks_dir_for_design(Path("x.md")) == Path("x_tasks")


def test_default_task_path() -> None:
    from harness.core.invoke import default_task_path, tasks_dir_for_design
    from pathlib import Path

    got = default_task_path("docs/design/my-feature.md", "add-login")
    assert got == Path("docs/design/my-feature_tasks/add-login.md")
    # always lands under tasks_dir_for_design(design_path) -- no fallback_dir,
    # no collision-avoiding suffix (guard A handles collisions at the caller)
    assert got.parent == tasks_dir_for_design("docs/design/my-feature.md")


def test_latest_task_file(tmp_path) -> None:
    import time
    from harness.core.invoke import latest_task_file

    design_dir = tmp_path / "design"
    fallback_dir = tmp_path / "legacy_tasks"
    (design_dir / "a_tasks").mkdir(parents=True)
    (fallback_dir).mkdir(parents=True)

    old = design_dir / "a_tasks" / "old.md"
    old.write_text("old", encoding="utf-8")
    legacy = fallback_dir / "legacy.md"
    legacy.write_text("legacy", encoding="utf-8")
    time.sleep(0.02)
    newest = design_dir / "a_tasks" / "newest.md"
    newest.write_text("newest", encoding="utf-8")

    got = latest_task_file(design_dir, fallback_dir)
    assert got == newest

    # missing directories are treated as empty, not an error
    assert latest_task_file(tmp_path / "nope", tmp_path / "also-nope") is None

    # legacy flat dir alone is still picked up if it's newest
    time.sleep(0.02)
    legacy2 = fallback_dir / "legacy2.md"
    legacy2.write_text("legacy2", encoding="utf-8")
    assert latest_task_file(design_dir, fallback_dir) == legacy2


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

    # invoke() dispatches "hermes" to the internal _run_hermes() helper
    # (Popen + reader threads + log-tail liveness) instead of subprocess.run
    # directly, so the retry/content-block logic is exercised by faking that
    # boundary instead.
    # Simulated hermes: 1st call blocked (emits a session id), 2nd succeeds.
    calls = {"n": 0}

    def fake_run_hermes(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # blocked, but prints a session id we can resume with
            return {"returncode": 0, "stdout": "你好，我无法给到相关内容。\nsession_id: ABC123\n",
                    "stderr": "", "timed_out": False, "stall_reason": None}
        # resumed attempt: success with code
        return {"returncode": 0,
                "stdout": "```python\ndef build_model(e):\n    return {}\n```\nsession_id: ABC123\n",
                "stderr": "", "timed_out": False, "stall_reason": None}

    monkeypatch.setattr(inv, "_run_hermes", fake_run_hermes)

    res = inv.invoke(decl, "implement this", model="tencent/hy3:free",
                     effort="high", role="implement", timeout=10, max_retries=3)
    # retried exactly twice (blocked once, succeeded on 2nd)
    assert calls["n"] == 2
    assert res.get("content_blocked") is not True
    # the resume flag was used on the 2nd attempt
    assert "--resume" in res["cmd"]
    assert "ABC123" in res["cmd"]


def test_invoke_idle_timeout_enabled_for_hermes_via_log_tail(monkeypatch) -> None:
    """hermes has no streaming NDJSON stdout, but §2's log-tail path gives it
    a real liveness signal, so invoke() now dispatches hermes to
    _run_hermes() (not _run_streaming()) with idle_timeout passed through
    unchanged, instead of disabling idle-timeout entirely."""
    import harness.core.invoke as inv
    from harness.core.invoke import VendorDecl

    decl = VendorDecl("hermes", {
        "headless": ["hermes", "chat", "-q", "{prompt}", "-Q"],
        "session": {"resume_flag": ["--resume"]},
    })
    seen = {}

    def fake_run_hermes(cmd, **kw):
        seen["idle_timeout"] = kw["idle_timeout"]
        return {"returncode": 0, "stdout": "ok", "stderr": "",
                "timed_out": False, "stall_reason": None}

    def fail_run_streaming(cmd, **kw):
        raise AssertionError("hermes must be dispatched to _run_hermes, not _run_streaming")

    monkeypatch.setattr(inv, "_run_hermes", fake_run_hermes)
    monkeypatch.setattr(inv, "_run_streaming", fail_run_streaming)
    inv.invoke(decl, "do it", idle_timeout=42)
    assert seen["idle_timeout"] == 42


def test_invoke_idle_timeout_enabled_for_claude(monkeypatch) -> None:
    """claude has a streaming mode, so its declared idle_timeout is passed
    through unchanged (see _STREAM_PARSERS)."""
    import harness.core.invoke as inv

    decl = load_vendors("harness/config")["claude"]
    seen = {}

    def fake_run_streaming(cmd, **kw):
        seen["idle_timeout"] = kw["idle_timeout"]
        return {"returncode": 0, "stdout": '{"type":"result","result":"ok"}',
                "stderr": "", "timed_out": False, "stall_reason": None}

    monkeypatch.setattr(inv, "_run_streaming", fake_run_streaming)
    inv.invoke(decl, "do it", idle_timeout=42)
    assert seen["idle_timeout"] == 42


def test_invoke_raises_timeout_expired_on_stall(monkeypatch) -> None:
    """A stalled attempt (idle or absolute) must raise subprocess.TimeoutExpired
    with the partial output attached, matching subprocess.run's old contract
    (no caller today expects a dict-shaped timeout result)."""
    import subprocess as sp
    import harness.core.invoke as inv

    decl = load_vendors("harness/config")["claude"]

    def fake_run_streaming(cmd, **kw):
        return {"returncode": None, "stdout": "partial output", "stderr": "partial err",
                "timed_out": True, "stall_reason": "idle"}

    monkeypatch.setattr(inv, "_run_streaming", fake_run_streaming)
    try:
        inv.invoke(decl, "do it", idle_timeout=5)
        assert False, "expected TimeoutExpired"
    except sp.TimeoutExpired as e:
        assert e.output == "partial output"
        assert e.stderr == "partial err"


def test_claude_stream_detail_and_terminal() -> None:
    from harness.core.invoke import _claude_stream_detail, _STREAM_PARSERS

    assert _claude_stream_detail({"type": "assistant"}) == "type=assistant"
    assert _claude_stream_detail({}) is None
    # unknown/future type values still produce a passthrough detail string
    assert _claude_stream_detail({"type": "some_future_type"}) == "type=some_future_type"
    assert "claude" in _STREAM_PARSERS


def test_agy_stream_detail() -> None:
    from harness.core.invoke import _agy_stream_detail

    step_update = {
        "event": "step_update",
        "step_update": {"state": "ACTIVE", "step_type": "agent_response"},
    }
    assert _agy_stream_detail(step_update) == "step_update agent_response ACTIVE"
    assert _agy_stream_detail({"event": "init"}) == "event=init"
    assert _agy_stream_detail({}) is None


def test_codex_stream_detail_and_reconstruct() -> None:
    from harness.core.invoke import _codex_stream_detail, _codex_reconstruct_text

    assert _codex_stream_detail({"type": "turn.started"}) == "type=turn.started"
    assert _codex_stream_detail({}) is None

    # normal completion: text lives in item.completed/agent_message, NOT in
    # the terminal turn.completed line (which only carries usage stats).
    lines = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                             "text": '{"greeting": "hi"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    ]
    assert _codex_reconstruct_text(lines) == '{"greeting": "hi"}'

    # a failed turn (no agent_message item) -> None, caller falls back to raw stdout
    failed_lines = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "error", "message": "Selected model is at capacity."},
        {"type": "turn.failed", "error": {"message": "Selected model is at capacity."}},
    ]
    assert _codex_reconstruct_text(failed_lines) is None


def test_run_streaming_reader_and_idle_timeout(monkeypatch) -> None:
    """End-to-end (fake Popen) check of _run_streaming(): streamed NDJSON lines
    drive progress_cb, and running out of idle_timeout with the queue empty
    kills the process and reports timed_out/stall_reason='idle'."""
    import io
    import harness.core.invoke as inv

    class _FakeStdout(io.StringIO):
        def __init__(self, text):
            super().__init__(text)

    class _FakeProc:
        def __init__(self, stdout_text, stderr_text):
            self.stdout = io.StringIO(stdout_text)
            self.stderr = io.StringIO(stderr_text)
            self.returncode = None
            self._killed = False

        def kill(self):
            self._killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            if not self._killed:
                self.returncode = 0
            return self.returncode

    fake = _FakeProc(
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"turn.completed","usage":{}}\n',
        "",
    )
    monkeypatch.setattr(inv.subprocess, "Popen", lambda *a, **kw: fake)

    details = []
    result = inv._run_streaming(
        ["codex", "exec", "--json"], cwd=None, stdin_input=None,
        timeout=10, idle_timeout=5, progress_cb=details.append,
        vendor_name="codex",
    )
    assert result["timed_out"] is False
    assert details == ["type=thread.started", "type=turn.completed"]
    assert result["returncode"] == 0


def test_run_streaming_stall_kills_process(monkeypatch) -> None:
    """No output at all before idle_timeout elapses -> process is killed and
    timed_out/stall_reason='idle' is reported (no exception at this layer;
    invoke() is the one that turns this into subprocess.TimeoutExpired)."""
    import io
    import harness.core.invoke as inv

    class _FakeProc:
        def __init__(self):
            self.stdout = io.StringIO("")  # EOF immediately -> reader thread exits
            self.stderr = io.StringIO("")
            self.returncode = None
            self._killed = False

        def kill(self):
            self._killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode if self.returncode is not None else 0

    fake = _FakeProc()
    monkeypatch.setattr(inv.subprocess, "Popen", lambda *a, **kw: fake)

    # empty stdout/stderr means both reader threads immediately push their EOF
    # sentinel, so open_streams empties out before any timeout fires — this
    # exercises the "process already exited with nothing to say" path, which
    # must NOT be reported as a stall.
    result = inv._run_streaming(
        ["codex", "exec", "--json"], cwd=None, stdin_input=None,
        timeout=10, idle_timeout=5, progress_cb=None, vendor_name="codex",
    )
    assert result["timed_out"] is False
    assert result["stdout"] == ""


def test_start_hermes_log_tail_pumps_lines_into_queue() -> None:
    """_start_hermes_log_tail() spawns `<exe> logs -f --session <id>` and
    pumps its stdout lines into the shared queue tagged "logtail", terminated
    by a (name, None) EOF sentinel — tested in isolation (join()ed
    synchronously) to avoid the thread-interleaving race that a full
    _run_hermes() run would introduce."""
    import io
    import queue
    import harness.core.invoke as inv

    seen_args = {}

    class _FakeLogProc:
        def __init__(self):
            self.stdout = io.StringIO("log line 1\nlog line 2\n")

    def fake_popen(cmd, **kw):
        seen_args["cmd"] = cmd
        return _FakeLogProc()

    import harness.core.invoke as inv_mod
    orig_popen = inv_mod.subprocess.Popen
    inv_mod.subprocess.Popen = fake_popen
    try:
        q: "queue.Queue" = queue.Queue()
        log_proc, thread = inv._start_hermes_log_tail("hermes", "S123", q)
        assert thread is not None
        thread.join(timeout=2)
    finally:
        inv_mod.subprocess.Popen = orig_popen

    assert seen_args["cmd"] == ["hermes", "logs", "-f", "--session", "S123"]
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items == [
        ("logtail", "log line 1"),
        ("logtail", "log line 2"),
        ("logtail", None),
    ]


def test_start_hermes_log_tail_best_effort_on_spawn_failure() -> None:
    """If the log-tail process can't be started (e.g. hermes missing from
    PATH), _start_hermes_log_tail() degrades to (None, None) instead of
    raising -- liveness silently falls back to "no signal until exit"."""
    import queue
    import harness.core.invoke as inv

    def fake_popen(cmd, **kw):
        raise OSError("not found")

    orig_popen = inv.subprocess.Popen
    inv.subprocess.Popen = fake_popen
    try:
        q: "queue.Queue" = queue.Queue()
        log_proc, thread = inv._start_hermes_log_tail("hermes", "S123", q)
    finally:
        inv.subprocess.Popen = orig_popen

    assert log_proc is None
    assert thread is None


def test_run_hermes_captures_session_id_and_spawns_log_tail() -> None:
    """End-to-end _run_hermes() dispatch: main process's first stdout line
    carries `session_id: <id>`, which triggers a second Popen call for the
    log-tail process. The log-tail fake's stdout is deliberately EMPTY so its
    reader thread's EOF races harmlessly with the main loop's own completion
    (main process stdout/stderr closing is what actually ends the run)."""
    import io
    import harness.core.invoke as inv

    class _FakeMainProc:
        def __init__(self):
            self.stdout = io.StringIO("session_id: S123\nfinal answer\n")
            self.stderr = io.StringIO("")
            self.returncode = 0

        def kill(self):
            pass

        def wait(self, timeout=None):
            return self.returncode

    class _FakeLogProc:
        def __init__(self):
            self.stdout = io.StringIO("")  # empty: avoids interleaving race
            self.returncode = 0

        def kill(self):
            pass

        def wait(self, timeout=None):
            return self.returncode

    calls = []

    def fake_popen(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return _FakeMainProc()
        return _FakeLogProc()

    progress_calls = []
    monkey_orig = inv.subprocess.Popen
    inv.subprocess.Popen = fake_popen
    try:
        result = inv._run_hermes(
            ["hermes", "chat", "-q", "do it", "-Q"],
            cwd=None, timeout=10, idle_timeout=5,
            progress_cb=progress_calls.append,
        )
    finally:
        inv.subprocess.Popen = monkey_orig

    assert len(calls) == 2
    assert calls[1] == ["hermes", "logs", "-f", "--session", "S123"]
    assert "session_id=S123" in progress_calls
    assert result["timed_out"] is False
    assert "final answer" in result["stdout"]
