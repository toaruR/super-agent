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
    return [e["type"] for e in Ledger(path).load()]


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
