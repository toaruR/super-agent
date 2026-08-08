#!/usr/bin/env python
"""Tests for the Drive role (Stage B, serial orchestration)."""
from __future__ import annotations

from unittest import mock

from harness.roles import drive
from harness.core.invoke import resolve_role_channels

SAMPLE_TASKS = "probe/sample/my-design-tasks.md"


def _default_channels():
    # 設定(roles.implement)に依存しない形で期待チャンネル数を得る
    return resolve_role_channels("implement", config_dir="harness/config")



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
    n_tasks = len(out["tasks"])
    assert n_tasks == 2
    # Default (non-speculative): each task is implemented in a SINGLE channel.
    assert m_impl.call_count == n_tasks
    assert m_rev.call_count == n_tasks
    assert m_int.call_count == n_tasks
    int_calls = [c.args[0] for c in m_int.call_args_list]
    assert any("T1" in t for t in int_calls) and any("T2" in t for t in int_calls)

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
        # vendor/reviewer fall back to roles.* (not a hardcoded fallback).
        # With the default multi-channel implement list, each channel uses a
        # vendor from roles.implement; reviewer resolves to roles.review.vendor.
        configured = {c["vendor"] for c in resolve_role_channels("implement", config_dir="harness/config")}
        observed = {c.kwargs["vendor"] for c in m_impl.call_args_list}
        assert observed <= configured  # every impl used a configured implement vendor
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


def test_drive_speculative_fanout_and_winner_integration() -> None:
    # Stage B parallel (b) SPECULATIVE mode (opt-in): implement を複数チャンネルで
    # 並列実行し、最初に review を通したチャンネルを統合する。
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
                          implement_channels=channels, speculative=True)

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
    # Default (non-speculative): each task implemented in a SINGLE channel,
    # but the two INDEPENDENT tasks (PA, PB) run concurrently.
    assert m_impl.call_count == 2
    # both integrated (serial, but both present)
    assert m_int.call_count == 2


def test_drive_default_is_single_channel_not_speculative() -> None:
    """Regression: the default drive (no --speculative) must implement each task
    in exactly ONE channel, even though roles.implement declares 5 channels."""
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement") as m_impl, \
         mock.patch.object(drive, "run_pipeline") as m_rev, \
         mock.patch.object(drive, "integrate", return_value={"ok": True}), \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": [], "depends_on": []},
             {"task_id": "T2", "goal": "g", "acceptance": [], "touch_allow": [], "depends_on": []},
         ]):
        m_impl.return_value = {"ok": True, "commit": "c1"}
        m_rev.return_value = {"verdict": "pass"}
        out = drive.drive("", None, "probe/sample/my-design-tasks.md",
                          seq=None, dry_run=False)
    assert out["ok"] is True
    # 2 tasks, each in a single channel => 2 impl calls (NOT 2 * 5)
    assert m_impl.call_count == 2
    assert m_rev.call_count == 2
    # every channel id used is the plain task id (no __vendor_N composite)
    for c in m_impl.call_args_list:
        tid = c.args[0]
        assert "__" not in tid


def test_drive_speculative_flag_fans_out_all_channels() -> None:
    """With speculative=True, the full roles.implement fan-out is used."""
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement") as m_impl, \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "fail"}), \
         mock.patch.object(drive, "integrate", return_value={"ok": True}), \
         mock.patch.object(drive, "create_worktree", return_value={"ok": True}), \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": [], "depends_on": []},
         ]):
        m_impl.return_value = {"ok": True, "commit": "c1"}
        drive.drive("", None, "probe/sample/my-design-tasks-parallel.md",
                    seq=None, dry_run=False, speculative=True)
    # all 5 declared channels are used for the single task
    n_ch = len(_default_channels())
    assert m_impl.call_count == n_ch
    # composite channel ids appear (speculative fan-out)
    assert any("__" in c.args[0] for c in m_impl.call_args_list)


def test_drive_default_single_channel_creates_worktree() -> None:
    """Regression (bug: single-channel path skipped create_worktree, causing
    [WinError 267] when the vendor ran in a non-existent cwd). The default
    (non-speculative) path must still create a worktree before implementing."""
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "cX"}) as m_impl, \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
         mock.patch.object(drive, "integrate", return_value={"ok": True}), \
         mock.patch.object(drive, "create_worktree", return_value={"ok": True, "path": "workspaces/T1", "branch": "task/T1"}) as m_cw, \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": [], "depends_on": []},
         ]):
        out = drive.drive("", None, "probe/sample/my-design-tasks.md",
                          seq=None, dry_run=False)
    assert out["ok"] is True
    # create_worktree must be called for the single (plain) task id
    assert m_cw.called
    assert any("T1" in (c.args[0] if c.args else "") for c in m_cw.call_args_list)
    # implement runs inside the created worktree
    assert m_impl.called


def test_drive_adaptive_calls_planner_replan() -> None:
    """With adaptive=True (default), drive invokes the planner role between
    layers to re-examine the DAG. The planner's revisit is what lets us carve
    out investigation tasks / merge over-split tasks at execution time."""
    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "c1"}), \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
         mock.patch.object(drive, "integrate", return_value={"ok": True}), \
         mock.patch.object(drive, "create_worktree", return_value={"ok": True}), \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
             {"task_id": "T2", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
         ]), \
         mock.patch.object(drive, "planner_role") as m_planner, \
         mock.patch.object(drive, "resolve_role", return_value={"vendor": "claude", "model": None}), \
         mock.patch.object(drive, "Sequencer") as m_seq_cls:
        m_seq = m_seq_cls.return_value
        m_seq.load.return_value = []
        m_planner.replan.return_value = {
            "ok": True,
            "tasks": [
                {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
                {"task_id": "T2", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
            ],
            "investigation_needed": [],
            "notes": "",
        }
        drive.drive("", None, "probe/sample/my-design-tasks.md",
                    seq=m_seq, dry_run=False, adaptive=True)
    assert m_planner.replan.called, "planner.replan must be called in adaptive mode"


def test_drive_checks_out_target_branch_before_integrate() -> None:
    """Regression: integrate() merges into the repo root's CURRENT branch, so
    drive must check out target_branch first (and restore the caller's branch
    afterwards) to avoid merging onto the wrong branch / surprising side effects."""
    import subprocess as _sp
    captured = []
    real_run = _sp.run

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "rev-parse"] and "--abbrev-ref" in cmd:
            # pretend we are on feat/planner before drive touches anything
            class _R:
                returncode = 0
                stdout = "feat/planner\n"
                stderr = ""
            return _R()
        if cmd[:2] == ["git", "checkout"]:
            captured.append(cmd[2])
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        return real_run(cmd, *a, **k)

    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "c1"}), \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
         mock.patch.object(drive, "integrate", return_value={"ok": True}), \
         mock.patch.object(drive, "create_worktree", return_value={"ok": True}), \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(drive, "parse_tasks_md", return_value=[
             {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
         ]), \
         mock.patch.object(drive, "planner_role") as m_planner, \
         mock.patch.object(drive, "Sequencer") as m_seq_cls, \
         mock.patch.object(_sp, "run", side_effect=fake_run):
        m_seq = m_seq_cls.return_value
        m_seq.load.return_value = []
        m_planner.replan.return_value = {"ok": True, "tasks": [
            {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
        ], "investigation_needed": [], "notes": ""}
        drive.drive("", None, "probe/sample/my-design-tasks.md",
                    seq=m_seq, dry_run=False, target_branch="feat/dashboard")
    assert "feat/dashboard" in captured, f"expected checkout feat/dashboard, got {captured}"
    assert captured[-1] == "feat/planner", f"expected restore to feat/planner, got {captured}"
