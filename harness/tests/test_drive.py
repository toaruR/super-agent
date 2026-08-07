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

def test_drive_resolves_vendors_from_roles_yaml_when_unspecified() -> None:
    """Stage B consolidation (A/B/C): drive must resolve implement/reviewer
    vendors from vendors.yaml `roles:` defaults, not hardcoded fallbacks."""
    with mock.patch.object(drive, "structural_check", return_value=[]),          mock.patch.object(drive, "implement") as m_impl,          mock.patch.object(drive, "run_pipeline") as m_rev,          mock.patch.object(drive, "integrate", return_value={"ok": True}),          mock.patch.object(drive, "schedule"),          mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": [], "depends_on": []}
         ]):
        m_impl.return_value = {"ok": True, "commit": "c1"}
        m_rev.return_value = {"verdict": "pass"}
        out = drive.drive("", None, "probe/sample/my-design-tasks.md",
                          seq=None, dry_run=False)
        assert out["ok"] is True
        # vendor falls back to roles.implement.vendor (agy) and roles.review.vendor (codex)
        assert m_impl.call_args.kwargs["vendor"] == "agy"
        assert m_rev.call_args.kwargs["reviewer_vendor"] == "codex"


def test_drive_respects_explicit_vendor_override() -> None:
    with mock.patch.object(drive, "structural_check", return_value=[]),          mock.patch.object(drive, "implement") as m_impl,          mock.patch.object(drive, "run_pipeline") as m_rev,          mock.patch.object(drive, "integrate", return_value={"ok": True}),          mock.patch.object(drive, "schedule"),          mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": [], "depends_on": []}
         ]):
        m_impl.return_value = {"ok": True, "commit": "c1"}
        m_rev.return_value = {"verdict": "pass"}
        out = drive.drive("", None, "probe/sample/my-design-tasks.md",
                          seq=None, dry_run=False,
                          implement_vendor="hermes", reviewer_vendor="claude")
        assert out["ok"] is True
        assert m_impl.call_args.kwargs["vendor"] == "hermes"
        assert m_rev.call_args.kwargs["reviewer_vendor"] == "claude"


def test_drive_skips_integrate_when_review_fails() -> None:
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", return_value={"ok": True}), \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "fail"}), \
         mock.patch.object(drive, "integrate") as m_int:
        out = drive.drive("", None, SAMPLE_TASKS, seq=None, dry_run=False)

    assert out["ok"] is True
    m_int.assert_not_called()
    assert out["tasks"][0]["integrate"]["skipped"] is True


def test_drive_multichannel_fanout_and_winner_integration() -> None:
    # Stage B parallel (b): implement を複数チャンネルで並列実行し、
    # 最初に review を通したチャンネルを統合する。
    channels = [
        {"vendor": "agy", "model": "gemini-3.6-flash", "effort": "high"},
        {"vendor": "hermes", "model": "hy3:Free", "effort": "high"},
        {"vendor": "hermes", "model": "hy3:Free", "effort": "high"},
    ]
    with mock.patch.object(drive, "structural_check", return_value=[]),          mock.patch.object(drive, "implement") as m_impl,          mock.patch.object(drive, "run_pipeline") as m_rev,          mock.patch.object(drive, "integrate") as m_int,          mock.patch.object(drive, "create_worktree") as m_wt,          mock.patch.object(drive, "schedule"):
        # channel 0 (agy) passes review; the rest fail
        m_impl.return_value = {"ok": True, "commit": "cX"}
        def _rev(*a, **k):
            tid = a[0]
            return {"verdict": "pass" if "agy_0" in tid else "fail"}
        m_rev.side_effect = _rev
        m_int.return_value = {"ok": True, "commit": "cInt"}

        out = drive.drive("", None, SAMPLE_TASKS, seq=None, dry_run=False,
                          implement_channels=channels)

    assert out["ok"] is True
    t1 = next(t for t in out["tasks"] if t["task_id"] == "T1")
    # 3 channels implemented in parallel (one impl call per channel)
    assert len(t1["implement"]["channels"]) == 3
    # review ran per channel
    assert len(t1["review"]["channels"]) == 3
    # winner is the agy channel (first to pass)
    assert t1["integrate"]["winner"] == "agy"
    # integrate called once, with the winning composite task id
    assert m_int.call_count == 2  # T1 + T2
    winner_tid = m_int.call_args_list[0].args[0]
    assert "agy_0" in winner_tid


def test_topo_layers_partitions_independent_tasks() -> None:
    from harness.roles.scheduler import topo_layers
    tasks = [
        {"task_id": "A", "depends_on": []},
        {"task_id": "B", "depends_on": ["A"]},
        {"task_id": "C", "depends_on": []},   # independent of A/B
        {"task_id": "D", "depends_on": ["B", "C"]},
    ]
    layers = topo_layers(tasks)
    # layer 0 = independent (A, C); layer 1 = B (needs A); layer 2 = D
    assert set(layers[0]) == {"A", "C"}
    assert layers[1] == ["B"]
    assert layers[2] == ["D"]


def test_drive_parallel_tasks_runs_independent_concurrently() -> None:
    # Stage B task-level: --parallel-tasks で独立タスクを同時に implement+review。
    # ここでは呼び出し回数と順序のみ検証（実 vendor はモック）。
    tasks_md = "probe/sample/my-design-tasks-parallel.md"  # PA, PB (独立)
    with mock.patch.object(drive, "structural_check", return_value=[]),          mock.patch.object(drive, "implement") as m_impl,          mock.patch.object(drive, "run_pipeline") as m_rev,          mock.patch.object(drive, "integrate") as m_int,          mock.patch.object(drive, "schedule"):
        m_impl.return_value = {"ok": True, "commit": "cX"}
        m_rev.return_value = {"verdict": "pass"}
        m_int.return_value = {"ok": True, "commit": "cI"}

        out = drive.drive("", None, tasks_md, seq=None, dry_run=False,
                          parallel_tasks=True)
    assert out["ok"] is True
    assert len(out["tasks"]) == 2
    # both tasks implemented (single channel each -> 1 impl call per task)
    assert m_impl.call_count == 2
    # both integrated (serial, but both present)
    assert m_int.call_count == 2
