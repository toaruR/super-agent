#!/usr/bin/env python
"""Stage 0 CLI tests: review / log / show drive the pipeline and ledger."""
from __future__ import annotations

import json
import subprocess
import sys
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


def test_review_task_handoff_resolves_worktree_and_acceptance(tmp_path, monkeypatch):
    """Stage 5 handoff: `review --task T1 --tasks dag.md` resolves acceptance
    and the worktree path from the implemented task (no live vendor)."""
    monkeypatch.chdir(REPO)
    # write a task DAG with T1 having a custom acceptance
    dag = tmp_path / "tasks.md"
    dag.write_text(
        "# タスク分解\n要求: demo\nタスク数: 1\n\n"
        "## 1. T1\n\n- 目標: g\n- 依存: （なし）\n"
        "- 触ってよい範囲: wclite/core.py\n"
        "- 受入基準 (1):\n  - `pytest` tests/test_core.py (expect_exit=0)\n",
        encoding="utf-8")
    # stage a worktree dir so target.exists() passes
    wt = REPO / "workspaces" / "T1"
    wt.mkdir(parents=True, exist_ok=True)
    try:
        ledger = REPO / "harness" / "ledger" / "events.jsonl"
        if ledger.exists():
            ledger.unlink()
        res = _run("review", "--task", "T1", "--tasks", str(dag),
                   "--reviewer", "codex", "--dry-run")
        j = json.loads(res.stdout)
        assert j["tree_hash"], "tree_hash must be bound (CVE ran)"
        assert j["verdict"] in ("pass", "fail", "judgment_unavailable", "environment_error")
        # the task id is reused (T1), not a fresh T-xxxx
        lg = ledger.read_text(encoding="utf-8")
        assert "T1:" in lg
        assert "verification.run" in lg
    finally:
        import shutil
        shutil.rmtree(wt, ignore_errors=True)
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


def test_drive_speculative_flag_is_accepted(monkeypatch):
    """The --speculative flag must be accepted by the CLI parser and forwarded
    to drive() (no error). Use --dry-run so no live vendor is invoked."""
    monkeypatch.chdir(REPO)
    dag = REPO / "probe" / "sample" / "my-design-tasks-parallel.md"
    res = _run("drive", "--tasks", str(dag), "--speculative", "--dry-run")
    # dry-run drive prints a JSON summary with ok=True
    import json as _json
    j = _json.loads(res.stdout)
    assert j["ok"] is True


def test_drive_default_has_no_speculative_flag_in_help(monkeypatch):
    """Sanity: --speculative appears in drive's help (so it is wired up)."""
    monkeypatch.chdir(REPO)
    res = _run("drive", "--help")
    assert "--speculative" in res.stdout
