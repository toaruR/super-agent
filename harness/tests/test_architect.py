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


def _run(*cli_args, expect_rc=0):
    cmd = [CVE, *CLI, *cli_args]
    res = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    assert res.returncode == expect_rc, f"rc={res.returncode} stderr={res.stderr}"
    return res


def test_architect_spec_records_adr(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    spec = tmp_path / "my-design.md"
    spec.write_text("Web API は FastAPI で作る。認証は JWT。", encoding="utf-8")
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    res = _run("architect", "Web API を作れ", "--design_file", str(spec))
    adr = json.loads(res.stdout)
    assert adr["source"] == "human"
    assert "FastAPI" in adr["decisions"][0]["decision"]
    # ledger has adr.written
    lg = ledger.read_text(encoding="utf-8")
    assert "adr.written" in lg
    assert "FastAPI" in lg


def test_architect_spec_missing_creates_via_llm_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    missing = tmp_path / "new-design.md"
    assert not missing.exists()
    # dry_run path: assembles the prompt and reports it without calling vendor
    res = _run("architect", "Excel からオントロジーを作れ", "--design_file", str(missing), "--dry-run")
    adr = json.loads(res.stdout)
    assert adr["source"] == "llm(dry)"
    assert "cmd" in adr
    # file still not created in dry-run
    assert not missing.exists()


def test_architect_log_shows_adr(monkeypatch):
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    spec = REPO / "probe" / "n3" / "caseGreen" / "tests" / "test_ok.py"
    _run("architect", "demo", "--design_file", str(spec))
    chunk = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    first_id = chunk["events"][0]["event_id"].split(":")[0]
    out = _run("log", first_id).stdout
    assert "adr.written" in out


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
