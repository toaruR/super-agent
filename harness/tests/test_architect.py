#!/usr/bin/env python
"""Stage 1 (architect) tests: --design_file records verbatim, --dry-run assembles prompt."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
import os
from pathlib import Path
CVE = os.environ.get(
    "CVE_PYTHON",
    str(Path(__file__).resolve().parents[2] / ".cve-venv" / "Scripts" / "python.exe"),
)
CLI = ["-m", "harness.cli"]


def _run(*cli_args, expect_rc=0, env=None):
    cmd = [CVE, *CLI, *cli_args]
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    res = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, env=run_env)
    assert res.returncode == expect_rc, f"rc={res.returncode} stderr={res.stderr}"
    return res


def _cleanup_test_design_files():
    import glob
    targets = ["demo*.md", "Web-API-*.md", "Excel-*.md", "my-design*.md", "ダミー*.md", "素のテキスト*.md", "*1行*.md"]
    for pattern in targets:
        for f in glob.glob(str(REPO / "docs" / "design" / pattern)):
            try:
                Path(f).unlink()
            except OSError:
                pass


def test_architect_spec_records_adr(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    spec = tmp_path / "my-design.md"
    spec.write_text("Web API は FastAPI で作る。認証は JWT。", encoding="utf-8")
    ledger = tmp_path / "events.jsonl"
    try:
        res = _run("architect", "Web API を作れ", "--design_file", str(spec), env={"SUPER_AGENT_LEDGER": str(ledger)})
        adr = json.loads(res.stdout)
        assert adr["source"] == "human"
        assert "FastAPI" in adr["decisions"][0]["decision"]
        lg = ledger.read_text(encoding="utf-8")
        assert "adr.written" in lg
        assert "FastAPI" in lg
    finally:
        _cleanup_test_design_files()


def test_architect_spec_missing_creates_via_llm_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    missing = tmp_path / "new-design.md"
    assert not missing.exists()
    ledger = tmp_path / "events.jsonl"
    try:
        res = _run("architect", "Excel からオントロジーを作れ", "--design_file", str(missing), "--dry-run", env={"SUPER_AGENT_LEDGER": str(ledger)})
        adr = json.loads(res.stdout)
        assert adr["source"] == "llm(dry)"
        assert "cmd" in adr
        assert not missing.exists()
    finally:
        _cleanup_test_design_files()


def test_architect_log_shows_adr(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    spec = REPO / "probe" / "n3" / "caseGreen" / "tests" / "test_ok.py"
    try:
        _run("architect", "demo", "--design_file", str(spec), env={"SUPER_AGENT_LEDGER": str(ledger)})
        chunk = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        first_id = chunk["events"][0]["event_id"].split(":")[0]
        out = _run("log", first_id, env={"SUPER_AGENT_LEDGER": str(ledger)}).stdout
        assert "adr.written" in out
    finally:
        _cleanup_test_design_files()


def test_architect_writes_progress_heartbeat_during_llm_proposal(monkeypatch, tmp_path):
    """LLM 起案パス（spec_path 未指定）で progress/<task_id>.json が
    running -> done と更新されることを確認する（vendor はモックで即応答）。"""
    from harness.core.ledger import Sequencer
    from harness.core.progress import read_progress
    import harness.roles.architect as arch_mod

    def fake_invoke(decl, prompt, **kw):
        progress_cb = kw.get("progress_cb")
        if progress_cb is not None:
            progress_cb("thinking")
        return {"cmd": [decl.name], "returncode": 0,
                "result": {"decisions": [{"topic": "t", "decision": "d", "rationale": "r"}],
                          "open_questions": []}}

    monkeypatch.setattr(arch_mod, "invoke", fake_invoke)
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    adr = arch_mod.propose("T1", "何か作れ", "claude", seq=seq)
    seq.stop()

    assert adr["source"] == "llm"
    got = read_progress("T1", tmp_path / "events.jsonl")
    assert got is not None
    assert got["status"] == "done"
    assert got["detail"] == ""


def test_architect_writes_progress_error_on_vendor_timeout(monkeypatch, tmp_path):
    import subprocess as _sp
    from harness.core.ledger import Ledger, Sequencer
    from harness.core.progress import read_progress
    import harness.roles.architect as arch_mod

    def fake_invoke(decl, prompt, **kw):
        raise _sp.TimeoutExpired(cmd=[decl.name], timeout=1800)

    monkeypatch.setattr(arch_mod, "invoke", fake_invoke)
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    adr = arch_mod.propose("T1", "何か作れ", "claude", seq=seq)
    seq.stop()

    assert "error" in adr
    got = read_progress("T1", tmp_path / "events.jsonl")
    assert got["status"] == "error"
    evs = Ledger(str(tmp_path / "events.jsonl")).load_flat()
    types = {e["type"] for e in evs}
    assert "architect.error" in types


def test_architect_human_supplied_spec_does_not_write_progress(monkeypatch, tmp_path):
    """spec_path が既存ファイルの場合は同期的に読むだけなので、
    running のまま放置される progress ファイルは作られないこと。"""
    from harness.core.ledger import Sequencer
    from harness.core.progress import read_progress
    import harness.roles.architect as arch_mod

    spec = tmp_path / "my-design.md"
    spec.write_text("Web API は FastAPI で作る。", encoding="utf-8")
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    adr = arch_mod.propose("T1", "Web API を作れ", "claude", spec_path=str(spec), seq=seq)
    seq.stop()

    assert adr["source"] == "human"
    assert read_progress("T1", tmp_path / "events.jsonl") is None


def test_architect_draft_saved_and_resumed(tmp_path, monkeypatch):
    """途中失敗で .draft が作成され、再実行時にプロンプトへ引き継がれ、
    成功時に .draft が削除されることを確認する。"""
    import subprocess as _sp
    from harness.core.ledger import Sequencer
    from harness.core.invoke import atomic_write_draft
    import harness.roles.architect as arch_mod

    spec_path = tmp_path / "new-feature.md"
    draft_path = tmp_path / "new-feature.md.draft"

    prompts_captured = []

    def fake_invoke_fail(decl, prompt, **kw):
        prompts_captured.append(prompt)
        dp = kw.get("draft_path")
        if dp:
            atomic_write_draft(dp, "途中の思考ログ: API設計の素案")
        raise _sp.TimeoutExpired(cmd=[decl.name], timeout=1800)

    def fake_invoke_pass(decl, prompt, **kw):
        prompts_captured.append(prompt)
        return {"cmd": [decl.name], "returncode": 0,
                "result": {"decisions": [{"topic": "API", "decision": "FastAPI", "rationale": "Fast"}],
                          "open_questions": []}}

    # 1. First run: timeout failure, leaves .draft behind
    monkeypatch.setattr(arch_mod, "invoke", fake_invoke_fail)
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    res1 = arch_mod.propose("T1", "新規機能開発", "claude", spec_path=str(spec_path), seq=seq)
    seq.stop()

    assert "error" in res1
    assert draft_path.exists()
    assert "途中の思考ログ" in draft_path.read_text(encoding="utf-8")

    # 2. Second run: recovers .draft into prompt and succeeds
    monkeypatch.setattr(arch_mod, "invoke", fake_invoke_pass)
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    res2 = arch_mod.propose("T2", "新規機能開発", "agy", spec_path=str(spec_path), seq=seq)
    seq.stop()

    assert res2["source"] == "llm->file"
    assert spec_path.exists()
    assert not draft_path.exists()  # draft deleted after success
    assert "前回の試行で途中まで作成された設計ドラフト" in prompts_captured[-1]
    assert "途中の思考ログ: API設計の素案" in prompts_captured[-1]

