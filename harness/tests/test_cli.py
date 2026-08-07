#!/usr/bin/env python
"""Stage 0 CLI tests: review / log / show drive the pipeline and ledger."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CVE = r"D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe"
CLI = ["-m", "harness.cli"]
CASE = str(REPO / "probe" / "n3" / "caseGreen")


def _run(*cli_args, expect_rc=0):
    cmd = [CVE, *CLI, *cli_args]
    res = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    assert res.returncode == expect_rc, f"rc={res.returncode} stderr={res.stderr}"
    return res


def test_review_dry_run_writes_pipeline_events(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    # clean ledger so we count this task's events only
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    res = _run("review", CASE, "--reviewer", "codex", "--dry-run")
    j = json.loads(res.stdout)
    # dry_run still runs CVE; verdict reflects reviewer skip
    assert j["tree_hash"], "tree_hash must be bound"
    assert j["verdict"] in ("pass", "fail", "judgment_unavailable", "environment_error")
    # ledger has verification.run + judgment for this task
    lg = (REPO / "harness" / "ledger" / "events.jsonl").read_text(encoding="utf-8")
    assert "verification.run" in lg
    assert "judgment" in lg


def test_log_shows_task_events(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    res = _run("review", CASE, "--reviewer", "codex", "--dry-run")
    # recover the task id from the ledger
    lines = ledger.read_text(encoding="utf-8").splitlines()
    first_id = json.loads(lines[0])["event_id"].split(":")[0]
    out = _run("log", first_id).stdout
    assert "verification.run" in out
    assert "judgment" in out


def test_show_design_and_plan(monkeypatch):
    monkeypatch.chdir(REPO)
    d = _run("show", "design").stdout
    assert "ゴール" in d or "評価" in d
    pl = _run("show", "plan").stdout
    assert "Stage" in pl


def test_status_runs(monkeypatch):
    monkeypatch.chdir(REPO)
    out = _run("status").stdout
    assert "events in ledger" in out
