#!/usr/bin/env python
"""Tests for the Drive role (Stage B, serial orchestration)."""
from __future__ import annotations

from unittest import mock

from harness.roles import drive

SAMPLE_TASKS = "probe/sample/my-design-tasks.md"


def test_drive_calls_pipeline_per_task_in_order() -> None:
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement") as m_impl, \
         mock.patch.object(drive, "run_pipeline") as m_rev, \
         mock.patch.object(drive, "integrate") as m_int:
        m_impl.return_value = {"ok": True, "commit": "c1"}
        m_rev.return_value = {"verdict": "pass"}
        m_int.return_value = {"ok": True, "commit": "c2"}

        out = drive.drive("", None, SAMPLE_TASKS, seq=None, dry_run=False)

    assert out["ok"] is True
    # the sample has 2 tasks
    assert len(out["tasks"]) == 2
    assert m_impl.call_count == 2
    assert m_rev.call_count == 2
    assert m_int.call_count == 2
    int_calls = [c.args[0] for c in m_int.call_args_list]
    assert "T1" in int_calls and "T2" in int_calls


def test_drive_skips_integrate_when_review_fails() -> None:
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", return_value={"ok": True}), \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "fail"}), \
         mock.patch.object(drive, "integrate") as m_int:
        out = drive.drive("", None, SAMPLE_TASKS, seq=None, dry_run=False)

    assert out["ok"] is True
    m_int.assert_not_called()
    assert out["tasks"][0]["integrate"]["skipped"] is True
