#!/usr/bin/env python
"""Stage 1 (architect) tests: --spec records verbatim, --dry-run assembles prompt."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CVE = r"D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe"
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
    res = _run("architect", "Web API を作れ", "--spec", str(spec))
    adr = json.loads(res.stdout)
    assert adr["source"] == "human"
    assert "FastAPI" in adr["decisions"][0]["decision"]
    # ledger has adr.written
    lg = ledger.read_text(encoding="utf-8")
    assert "adr.written" in lg
    assert "FastAPI" in lg


def test_architect_dry_run_assembles_prompt_without_call(monkeypatch):
    monkeypatch.chdir(REPO)
    res = _run("architect", "Web API を作れ", "--dry-run")
    adr = json.loads(res.stdout)
    assert adr["source"] == "llm(dry)"
    assert "cmd" in adr  # the command that *would* run


def test_architect_log_shows_adr(monkeypatch):
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    spec = REPO / "probe" / "n3" / "caseGreen" / "tests" / "test_ok.py"
    _run("architect", "demo", "--spec", str(spec))
    first_id = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])["event_id"].split(":")[0]
    out = _run("log", first_id).stdout
    assert "adr.written" in out
