#!/usr/bin/env python
"""Tests for the Integrator role (Stage 5)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from harness.roles import integrator
from harness.core.ledger import Ledger, Sequencer


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _make_seq(tmp_path) -> tuple[Sequencer, str]:
    path = str(tmp_path / "events.jsonl")
    seq = Sequencer(path)
    seq.start()
    return seq, path


def _types(path) -> list[str]:
    return [e["type"] for e in Ledger(path).load_flat()]


def test_integrate_dry_run_emits_merge_plan() -> None:
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    with mock.patch.object(integrator, "_changed_files", return_value=["wclite/core.py"]), \
         mock.patch.object(integrator, "_git", return_value={"returncode": 0, "stdout": "", "stderr": ""}):
        out = integrator.integrate("T1", task, "/wt/T1", target_branch="main",
                                   seq=seq, dry_run=True)
    seq.stop()
    assert out["ok"] is True
    assert "integration.merge" in _types(path)
    assert "integrated" in _types(path)


def test_integrate_touch_violation_blocked() -> None:
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    with mock.patch.object(integrator, "_changed_files", return_value=["evil.py"]):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False)
    seq.stop()
    assert out["ok"] is False
    assert "integration.touch_violation" in _types(path)


def test_integrate_merge_conflict_records_conflict() -> None:
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    states = iter([
        {"returncode": 0, "stdout": "", "stderr": ""},   # checkout main
        {"returncode": 1, "stdout": "", "stderr": "CONFLICT"},  # merge fails
        {"returncode": 0, "stdout": "", "stderr": ""},   # merge --abort
    ])

    def fake_git(args, cwd, dry_run=False):
        return next(states)

    with mock.patch.object(integrator, "_changed_files", return_value=[]), \
         mock.patch.object(integrator, "_git", side_effect=fake_git):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False)
    seq.stop()
    assert out["ok"] is False
    assert "conflict" in _types(path)


def test_integrate_acceptance_failure_after_merge() -> None:
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"],
            "acceptance": [{"verb": "pytest", "args": ["tests/"]}]}
    merge_states = iter([
        {"returncode": 0, "stdout": "", "stderr": ""},   # checkout main
        {"returncode": 0, "stdout": "", "stderr": ""},   # merge ok
    ])

    def fake_git(args, cwd, dry_run=False):
        return next(merge_states)

    class FakeCVE:
        def __init__(self, *a, **k):
            pass

        def run(self, root, acceptance):
            return {"tree_hash": "abc", "cve_ok": False, "evidence": []}

    with mock.patch.object(integrator, "_changed_files", return_value=[]), \
         mock.patch.object(integrator, "_git", side_effect=fake_git), \
         mock.patch.object(integrator, "CVE", FakeCVE):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False)
    seq.stop()
    assert out["ok"] is False
    assert "integrated.failed" in _types(path)


def test_changed_files_returns_empty_when_worktree_missing() -> None:
    """Regression: _changed_files must not raise [WinError 267] when the
    worktree dir is already gone (e.g. integrate called after teardown)."""
    assert integrator._changed_files("workspaces/does-not-exist-xyz") == []


def test_integrate_extends_touch_allow_for_new_nonconflicting_file() -> None:
    """A new file outside touch_allow, that no other task's touch_allow
    overlaps, is auto-approved (アプローチ3) instead of rejected."""
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    other_task = {"task_id": "T2", "touch_allow": ["other/thing.py"]}
    with mock.patch.object(integrator, "_changed_files",
                           return_value=["wclite/core.py", "wclite/helper.py"]), \
         mock.patch.object(integrator, "_new_paths", return_value={"wclite/helper.py"}), \
         mock.patch.object(integrator, "_commit_extension", return_value={"ok": True}), \
         mock.patch.object(integrator, "_git",
                           return_value={"returncode": 0, "stdout": "", "stderr": ""}):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False,
                                   all_tasks=[task, other_task])
    seq.stop()
    assert out["ok"] is True
    types = _types(path)
    assert "integration.touch_allow_extended" in types
    assert "integration.touch_violation" not in types


def test_integrate_does_not_extend_for_modified_existing_file() -> None:
    """A modification to a pre-existing file outside touch_allow is never
    auto-approved, even if it isn't flagged as a conflict elsewhere."""
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    with mock.patch.object(integrator, "_changed_files", return_value=["evil.py"]), \
         mock.patch.object(integrator, "_new_paths", return_value=set()):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False,
                                   all_tasks=[task])
    seq.stop()
    assert out["ok"] is False
    assert "integration.touch_violation" in _types(path)
    assert "integration.touch_allow_extended" not in _types(path)


def test_integrate_does_not_extend_when_new_file_conflicts_with_other_task() -> None:
    """A new file is NOT auto-approved if another task's touch_allow already
    claims that path (or a directory scope containing it)."""
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    other_task = {"task_id": "T2", "touch_allow": ["wclite/helper.py"]}
    with mock.patch.object(integrator, "_changed_files",
                           return_value=["wclite/core.py", "wclite/helper.py"]), \
         mock.patch.object(integrator, "_new_paths", return_value={"wclite/helper.py"}):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False,
                                   all_tasks=[task, other_task])
    seq.stop()
    assert out["ok"] is False
    assert "integration.touch_violation" in _types(path)
    assert "integration.touch_allow_extended" not in _types(path)


def test_integrate_does_not_extend_when_all_tasks_not_provided() -> None:
    """Conservative default: without all_tasks, extension is never attempted
    even for a genuinely new file, since safety can't be verified."""
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    with mock.patch.object(integrator, "_changed_files",
                           return_value=["wclite/core.py", "wclite/helper.py"]), \
         mock.patch.object(integrator, "_new_paths", return_value={"wclite/helper.py"}):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False)
    seq.stop()
    assert out["ok"] is False
    assert "integration.touch_violation" in _types(path)


def test_integrate_extension_commit_failure_blocks_merge() -> None:
    """If staging/committing the extension candidate fails, the task is
    rejected (not silently merged without the new file)."""
    tmp = _tmp()
    seq, path = _make_seq(tmp)
    task = {"task_id": "T1", "touch_allow": ["wclite/core.py"], "acceptance": []}
    with mock.patch.object(integrator, "_changed_files",
                           return_value=["wclite/core.py", "wclite/helper.py"]), \
         mock.patch.object(integrator, "_new_paths", return_value={"wclite/helper.py"}), \
         mock.patch.object(integrator, "_commit_extension",
                           return_value={"ok": False, "error": "commit failed"}):
        out = integrator.integrate("T1", task, "/wt/T1", seq=seq, dry_run=False,
                                   all_tasks=[task])
    seq.stop()
    assert out["ok"] is False
    assert "integration.touch_violation" in _types(path)


def test_new_paths_returns_empty_when_worktree_missing() -> None:
    assert integrator._new_paths("workspaces/does-not-exist-xyz") == set()


def test_new_paths_excludes_build_artifacts() -> None:
    """Untracked build/test artifacts (a .gitignore gap away from leaking
    through) must never be treated as touch_allow extension candidates."""
    porcelain = "\n".join([
        "?? wclite/helper.py",
        "?? wclite/__pycache__/helper.cpython-311.pyc",
        "?? coverage.xml",
        "?? .coverage",
        "?? scratch.tmp",
        "?? run.log",
        "?? htmlcov/index.html",
        "?? .pytest_cache/v/cache/nodeids",
    ]) + "\n"
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(stdout=porcelain)
        with mock.patch.object(Path, "is_dir", return_value=True):
            new = integrator._new_paths("/wt/T1")
    assert new == {"wclite/helper.py"}
