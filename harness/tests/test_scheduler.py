#!/usr/bin/env python
"""Stage 3 (scheduler) tests: topo order, worktree creation, lease issuance."""
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


def test_create_worktree_reuses_existing_checked_out_branch(tmp_path):
    from harness.roles.scheduler import create_worktree
    # simulate: `git worktree list --porcelain` reports the branch already checked out
    def fake_git(*args, **kwargs):
        class R:
            returncode = 0
            stdout = f"worktree {tmp_path}/T1\nbranch refs/heads/task/T1\n"
            stderr = ""
        return R()
    (tmp_path / "T1").mkdir()
    wt = create_worktree("T1", root=str(tmp_path), git=fake_git)
    assert wt["ok"] is True
    assert wt.get("reused") is True
    assert wt["path"].replace("\\", "/") == f"{tmp_path.as_posix()}/T1"


def test_create_worktree_already_checked_out_then_prunes(tmp_path, monkeypatch):
    from harness.roles.scheduler import create_worktree
    # simulate the real Windows failure: branch "task/T1" is reported already
    # checked out at a path, and `git worktree add -b` fails with that message.
    # Our code should `git worktree prune` (no-op here) then retry without -b.
    state = {"first": True}
    def fake_git(*a, **k):
        cmd = a[0]
        class R:
            pass
        r = R()
        if cmd[1] == "list":
            r.returncode = 0
            r.stdout = f"worktree {tmp_path}/T1\nbranch refs/heads/task/T1\n"
            r.stderr = ""
            return r
        if cmd[1] == "prune":
            r.returncode = 0; r.stdout = ""; r.stderr = ""
            return r
        # worktree add
        if state["first"]:
            state["first"] = False
            r.returncode = 1
            r.stdout = ""
            r.stderr = "fatal: 'task/T1' is already checked out at '%s/T1'" % tmp_path
            return r
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r
    (tmp_path / "T1").mkdir()
    wt = create_worktree("T1", root=str(tmp_path), git=fake_git)
    assert wt["ok"] is True
    assert state["first"] is False  # it retried (prune + add w/o -b)


def test_create_worktree_branch_exists_no_checkout(tmp_path):
    from harness.roles.scheduler import create_worktree
    # simulate: `git worktree add -b` fails with "already exists", then add w/o -b succeeds
    calls = {"n": 0}
    def fake_git(*args, **kwargs):
        cmd = args[0]
        class R:
            pass
        r = R()
        if cmd[1] == "list":
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r
        r.returncode = 1 if calls["n"] == 0 else 0
        r.stdout = ""
        r.stderr = "fatal: a branch named 'task/T1' already exists" if calls["n"] == 0 else ""
        calls["n"] += 1
        return r
    wt = create_worktree("T1", root=str(tmp_path), git=fake_git)
    assert wt["ok"] is True
    assert calls["n"] == 2  # tried -b then without


def test_create_worktree_plan(monkeypatch):
    from harness.roles.scheduler import create_worktree

    def fake_git(*a, **k):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    wt = create_worktree("T1", root="workspaces", git=fake_git)
    assert wt["path"].replace("\\", "/") == "workspaces/T1"
    assert wt["branch"] == "task/T1"
    assert wt["ok"] is True
    assert wt["cmd"][:3] == ["git", "worktree", "add"]


def test_create_worktree_does_not_reuse_stale_worktree_on_different_path(tmp_path):
    # bug3 regression: a composite channel id (T1__agy_0) must NOT reuse a
    # stale single-channel worktree (T1) that happens to share the branch
    # prefix. It must create its own isolated path.
    from harness.roles.scheduler import create_worktree

    def fake_git(*a, **k):
        cmd = a[0]
        class R:
            pass
        r = R()
        r.returncode = 0
        if cmd[1] == "list":
            # a stale worktree on a DIFFERENT path (task/T1 checked out at T1)
            r.stdout = f"worktree {tmp_path}/T1\nbranch refs/heads/task/T1\n"
        elif cmd[1] == "prune":
            r.stdout = ""
        else:  # worktree add
            r.stdout = ""
        r.stderr = ""
        return r

    # the stale path exists but is NOT the path we are asking for
    (tmp_path / "T1").mkdir()
    wt = create_worktree("T1__agy_0", root=str(tmp_path), git=fake_git)
    assert wt["ok"] is True
    assert wt.get("reused") is not True
    assert wt["path"].replace("\\", "/") == f"{tmp_path.as_posix()}/T1__agy_0"


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
    evs = ledger.load_flat()
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


def test_teardown_worktree_idempotent() -> None:
    # create then teardown; second teardown must no-op (not error).
    from harness.roles.scheduler import create_worktree, teardown_worktree
    import subprocess, tempfile, os
    d = tempfile.mkdtemp()
    repo = os.path.join(d, "r")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    os.makedirs(os.path.join(repo, "workspaces"))
    r1 = create_worktree("TW", root=os.path.join(repo, "workspaces"))
    assert r1["ok"] is True
    t1 = teardown_worktree("TW", root=os.path.join(repo, "workspaces"))
    assert t1["ok"] is True and t1["removed"] is True
    # idempotent: worktree path gone -> branch deletion path only
    t2 = teardown_worktree("TW", root=os.path.join(repo, "workspaces"))
    assert t2["ok"] is True
