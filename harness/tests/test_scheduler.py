#!/usr/bin/env python
"""Stage 3 (scheduler) tests: topo order, worktree creation, lease issuance."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CVE = r"D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe"
CLI = ["-m", "harness.cli"]


def _run(*cli_args, expect_rc=0):
    res = subprocess.run([CVE, *CLI, *cli_args], cwd=str(REPO),
                         capture_output=True, text=True)
    assert res.returncode == expect_rc, f"rc={res.returncode} err={res.stderr}"
    return res


def test_topo_order_dependencies_first():
    from harness.roles.scheduler import topo_order
    tasks = [
        {"task_id": "T2", "depends_on": ["T1"]},
        {"task_id": "T1", "depends_on": []},
        {"task_id": "T3", "depends_on": ["T2"]},
    ]
    assert topo_order(tasks) == ["T1", "T2", "T3"]


def test_topo_order_cycle_safe():
    from harness.roles.scheduler import topo_order
    tasks = [
        {"task_id": "A", "depends_on": ["B"]},
        {"task_id": "B", "depends_on": ["A"]},
    ]
    # cyclic: still returns all ids without infinite loop
    assert set(topo_order(tasks)) == {"A", "B"}


def test_create_worktree_plan(monkeypatch):
    from harness.roles.scheduler import create_worktree
    wt = create_worktree("T1", root="workspaces", git=lambda *a, **k: None)
    assert wt["path"] == "workspaces/T1"
    assert wt["branch"] == "task/T1"
    assert wt["ok"] is True
    assert wt["cmd"][:3] == ["git", "worktree", "add"]


def test_schedule_dry_run_records_planned_worktree(monkeypatch):
    from harness.roles.scheduler import schedule
    from harness.core.ledger import Sequencer
    tasks = [
        {"task_id": "T1", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "depends_on": [], "touch_allow": ["src/a.py"]},
        {"task_id": "T2", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "depends_on": ["T1"], "touch_allow": ["src/b.py"]},
    ]
    seq = Sequencer(str(REPO / "harness" / "ledger" / "events.jsonl"))
    seq.start()
    res = schedule("T-plan", tasks, dry_run=True, seq=seq)
    seq.stop()
    assert res["ok"] is True
    from harness.core.ledger import Ledger
    ledger = Ledger(str(REPO / "harness" / "ledger" / "events.jsonl"))
    evs = [e for e in ledger.load() if e.get("task_id") == "T-plan" or e.get("type") == "task.scheduled"]
    assert any(e["type"] == "task.scheduled" for e in evs)


def test_plan_cli_dry_run(monkeypatch, tmp_path):
    monkeypatch.chdir(REPO)
    design = "# 設計: 単語数カウントCLIを作る\n\npytest のみで充分。\n"
    spec = tmp_path / "d.md"
    spec.write_text(design, encoding="utf-8")
    res = _run("plan", "--spec", str(spec), "--dry-run")
    out = json.loads(res.stdout)
    assert "decompose" in out and "schedule" in out
    assert out["schedule"]["ok"] is True
