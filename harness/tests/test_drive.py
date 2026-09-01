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



def test_drive_defaults_target_branch_to_design_branch_name() -> None:
    """No --target given: drive must derive the merge target from spec_path
    (design_branch_name) instead of falling back to a hardcoded "main" that
    git would silently auto-create."""
    from harness.roles.scheduler import design_branch_name

    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "c1"}), \
         mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
         mock.patch.object(drive, "integrate") as m_int:
        m_int.return_value = {"ok": True, "commit": "c2"}

        drive.drive("", "probe/sample/my-design.md", SAMPLE_TASKS, seq=None, dry_run=False)

    expected = design_branch_name("probe/sample/my-design.md")
    assert expected != "main"
    assert m_int.call_args.kwargs["target_branch"] == expected


def test_drive_reruns_reuse_same_plan_task_id(tmp_path) -> None:
    """Re-running drive() on the same (design_file, task_file) must register
    its plan/decompose bookkeeping event under the SAME task_id every time,
    not a fresh `plan-<slug>-<uuid>` per invocation -- otherwise every re-run
    leaves behind another permanent phantom row on the dashboard."""
    from harness.core.ledger import Sequencer, Ledger

    ledger_path = tmp_path / "events.jsonl"

    def _run_once():
        seq = Sequencer(str(ledger_path))
        seq.start()
        try:
            with mock.patch.object(drive, "structural_check", return_value=[]), \
                 mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "c1"}), \
                 mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
                 mock.patch.object(drive, "integrate") as m_int:
                m_int.return_value = {"ok": True, "commit": "c2"}
                out = drive.drive("", "probe/sample/my-design.md", SAMPLE_TASKS,
                                  seq=seq, dry_run=False, adaptive=False)
        finally:
            seq.stop()
        assert out["ok"] is True

    _run_once()
    _run_once()

    events = Ledger(str(ledger_path)).load_flat()
    plan_task_ids = {e["event_id"].split(":")[0]
                     for e in events if e["event_id"].split(":")[0].startswith("plan-")}
    assert len(plan_task_ids) == 1


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
        assert m_rev.call_args.kwargs["reviewer_vendor"] == "agy"


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
         mock.patch.object(drive, "create_worktree",
                          return_value={"ok": True, "path": "workspaces/T1"}), \
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
        m_seq.load_flat.return_value = []
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


def test_drive_replan_receives_flat_events_for_current_design_only(tmp_path) -> None:
    """Regression: replan() must receive a flat list of individual event
    dicts (each with a "type"), filtered to the current design_file --
    not the raw chunk list from Sequencer.load() (which has no "type" key
    at all and silently produced an empty summary), and not events from an
    unrelated design_file sharing the same ledger.

    See docs/plans/replan-events-summary-fix.md.
    """
    from harness.core.ledger import Ledger

    ledger_path = tmp_path / "events.jsonl"
    spec_path = "probe/sample/my-design.md"
    other_spec_path = str(tmp_path / "other-design.md")

    ledger = Ledger(str(ledger_path))
    ledger.append_event(spec_path, "", {"event_id": "T1:0", "type": "task.implemented", "task_id": "T1"})
    ledger.append_event(other_spec_path, "", {"event_id": "OX:0", "type": "integrated", "task_id": "OX"})

    from harness.core.ledger import Sequencer
    seq = Sequencer(str(ledger_path))
    seq.start()
    try:
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
             mock.patch.object(drive, "resolve_role", return_value={"vendor": "claude", "model": None}):
            m_planner.replan.return_value = {
                "ok": True,
                "tasks": [
                    {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
                ],
                "investigation_needed": [],
                "notes": "",
            }
            drive.drive("", spec_path, "probe/sample/my-design-tasks.md",
                        seq=seq, dry_run=False, adaptive=True)
    finally:
        seq.stop()

    assert m_planner.replan.called
    events_arg = m_planner.replan.call_args.kwargs["events"]
    assert all(isinstance(e.get("type"), str) and e["type"] for e in events_arg), \
        "events passed to replan must be individual event dicts with a type, not raw chunks"
    task_ids = {e.get("task_id") for e in events_arg}
    assert "T1" in task_ids
    assert "OX" not in task_ids, "events from an unrelated design_file must be filtered out"


def test_drive_integrates_each_layer_before_next_layer_starts() -> None:
    """Regression: integrate() for a topo layer's tasks must run BEFORE the
    next layer's implement() calls start, so a downstream task (T2 depends_on
    T1) is implemented against a worktree that already contains T1's merged
    output (previously integrate ran once, after ALL layers, so T2 could be
    implemented/reviewed before T1 was ever merged into target_branch)."""
    calls: list[str] = []

    def _impl(tid, *a, **k):
        calls.append(f"implement:{tid}")
        return {"ok": True, "commit": "c1"}

    def _rev(tid, *a, **k):
        calls.append(f"review:{tid}")
        return {"verdict": "pass"}

    def _integ(tid, *a, **k):
        calls.append(f"integrate:{tid}")
        return {"ok": True, "commit": "cInt"}

    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement", side_effect=_impl), \
         mock.patch.object(drive, "run_pipeline", side_effect=_rev), \
         mock.patch.object(drive, "integrate", side_effect=_integ), \
         mock.patch.object(drive, "create_worktree",
                          return_value={"ok": True, "path": "workspaces/T"}), \
         mock.patch.object(drive, "schedule"):
        out = drive.drive("", None, SAMPLE_TASKS, seq=None, dry_run=False)

    assert out["ok"] is True
    assert calls.index("integrate:T1") < calls.index("implement:T2"), calls


def _write_integrated_chunk(ledger_path, tasks_path: str, task_id: str, commit: str) -> None:
    from pathlib import Path as _Path
    from harness.core.ledger import Ledger
    task_file = str(_Path(tasks_path).resolve())
    Ledger(str(ledger_path)).append_chunk("", task_file, [
        {"event_id": f"{task_id}:0", "type": "integrated", "commit": commit,
         "branch": f"task/{task_id}", "target": "main"},
    ])


def test_drive_resume_skips_already_integrated_task(tmp_path) -> None:
    """Resume (default, resume=True): a task with a confirmed `integrated`
    ledger event whose commit is still an ancestor of target_branch must be
    skipped (not re-implemented/reviewed/integrated) on a re-run; a sibling
    task with no such event must still run normally."""
    import subprocess as _sp
    from harness.core.ledger import Sequencer

    tasks_md = "probe/sample/my-design-tasks-parallel.md"  # PA, PB (independent)
    ledger_path = tmp_path / "events.jsonl"
    _write_integrated_chunk(ledger_path, tasks_md, "PA", "deadbeef")
    seq = Sequencer(str(ledger_path))
    real_run = _sp.run

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "merge-base"]:
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        return real_run(cmd, *a, **k)

    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement") as m_impl, \
         mock.patch.object(drive, "run_pipeline") as m_rev, \
         mock.patch.object(drive, "integrate") as m_int, \
         mock.patch.object(drive, "create_worktree",
                          return_value={"ok": True, "path": "workspaces/PB"}), \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(_sp, "run", side_effect=fake_run):
        m_impl.return_value = {"ok": True, "commit": "c1"}
        m_rev.return_value = {"verdict": "pass"}
        m_int.return_value = {"ok": True, "commit": "cInt"}

        out = drive.drive("", None, tasks_md, seq=seq, dry_run=False, adaptive=False)

    assert out["ok"] is True
    pa = next(t for t in out["tasks"] if t["task_id"] == "PA")
    pb = next(t for t in out["tasks"] if t["task_id"] == "PB")
    assert pa["integrate"]["skipped"] is True
    assert pa["integrate"]["commit"] == "deadbeef"
    assert pb["integrate"]["ok"] is True
    # only PB (no prior integrated event) actually ran the pipeline
    assert m_impl.call_count == 1
    assert m_impl.call_args.args[0] == "PB"
    m_int.assert_called_once()


def test_drive_resume_false_forces_full_rerun(tmp_path) -> None:
    """--no-resume (resume=False) must ignore prior `integrated` ledger state
    and re-run every task, matching pre-resume-feature behavior."""
    import subprocess as _sp
    from harness.core.ledger import Sequencer

    tasks_md = SAMPLE_TASKS  # T1, T2
    ledger_path = tmp_path / "events.jsonl"
    _write_integrated_chunk(ledger_path, tasks_md, "T1", "deadbeef")
    seq = Sequencer(str(ledger_path))
    real_run = _sp.run

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "merge-base"]:
            raise AssertionError("merge-base must not be called when resume=False")
        return real_run(cmd, *a, **k)

    with mock.patch.object(drive, "structural_check", return_value=[]), \
         mock.patch.object(drive, "implement") as m_impl, \
         mock.patch.object(drive, "run_pipeline") as m_rev, \
         mock.patch.object(drive, "integrate") as m_int, \
         mock.patch.object(drive, "create_worktree",
                          return_value={"ok": True, "path": "workspaces/T"}), \
         mock.patch.object(drive, "schedule"), \
         mock.patch.object(_sp, "run", side_effect=fake_run):
        m_impl.return_value = {"ok": True, "commit": "c1"}
        m_rev.return_value = {"verdict": "pass"}
        m_int.return_value = {"ok": True, "commit": "cInt"}

        out = drive.drive("", None, tasks_md, seq=seq, dry_run=False,
                          adaptive=False, resume=False)

    assert out["ok"] is True
    assert m_impl.call_count == 2  # both T1 and T2 re-implemented
    assert m_int.call_count == 2


def test_drive_checks_out_target_branch_before_integrate() -> None:
    """Regression: integrate() merges into the repo root's CURRENT branch, so
    drive must check out target_branch first (and restore the caller's branch
    afterwards) to avoid merging onto the wrong branch / surprising side effects.

    This test verifies the REAL checkout path, so we temporarily disable the
    SUPER_AGENT_TEST guard (conftest sets it to keep the other tests from
    touching the dev's working tree).
    """
    import os as _os
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

    _old = _os.environ.pop("SUPER_AGENT_TEST", None)
    try:
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
            m_seq.load_flat.return_value = []
            m_planner.replan.return_value = {"ok": True, "tasks": [
                {"task_id": "T1", "goal": "g", "acceptance": [], "touch_allow": ["f.py"], "depends_on": []},
            ], "investigation_needed": [], "notes": ""}
            drive.drive("", None, "probe/sample/my-design-tasks.md",
                        seq=m_seq, dry_run=False, target_branch="feat/dashboard")
    finally:
        if _old is not None:
            _os.environ["SUPER_AGENT_TEST"] = _old
        else:
            _os.environ.pop("SUPER_AGENT_TEST", None)
    assert "feat/dashboard" in captured, f"expected checkout feat/dashboard, got {captured}"
    assert captured[-1] == "feat/planner", f"expected restore to feat/planner, got {captured}"


def _init_repo_with_design_and_tasks(tmp_path):
    """A real, isolated git repo with a design file + its task-DAG file
    already on disk (but not yet committed), for the design/task auto-commit
    and --push tests below (which disable SUPER_AGENT_TEST to exercise real
    git behavior and must never touch the dev's actual repo)."""
    import subprocess as _sp

    repo = tmp_path / "repo"
    repo.mkdir()
    _sp.run(["git", "init", "-q", str(repo)], check=True)
    _sp.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    _sp.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    _sp.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True)

    design = repo / "docs" / "design" / "my-design.md"
    design.parent.mkdir(parents=True)
    design.write_text("# 設計\n", encoding="utf-8")
    tasks_dir = repo / "docs" / "design" / "my-design_tasks"
    tasks_dir.mkdir()
    task_file = tasks_dir / "my-design.md"
    task_file.write_text(
        "# タスク分解（decompose 出力）\n\n要求: x\n\nタスク数: 1\n\n"
        "## 1. T1\n\n- 目標: g\n- 依存: （なし）\n- 触ってよい範囲: f.py\n",
        encoding="utf-8",
    )
    return repo, design, task_file


def test_drive_commits_design_and_task_files_onto_target_branch(tmp_path, monkeypatch) -> None:
    """The design file and its task-DAG dir must land on target_branch as a
    real commit (mirrors the "docs: add X design and task DAG" commits users
    used to make by hand), and re-running drive() on the same pair must not
    fail with "nothing to commit"."""
    import os as _os
    import subprocess as _sp

    repo, design, task_file = _init_repo_with_design_and_tasks(tmp_path)
    monkeypatch.chdir(repo)
    _old = _os.environ.pop("SUPER_AGENT_TEST", None)
    try:
        with mock.patch.object(drive, "structural_check", return_value=[]), \
             mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "c1"}), \
             mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
             mock.patch.object(drive, "integrate", return_value={"ok": True, "commit": "c2"}), \
             mock.patch.object(drive, "create_worktree", return_value={"ok": True, "path": str(repo)}), \
             mock.patch.object(drive, "schedule"):
            out1 = drive.drive("", str(design), str(task_file), seq=None, dry_run=False,
                                target_branch="design/my-design")
            out2 = drive.drive("", str(design), str(task_file), seq=None, dry_run=False,
                                target_branch="design/my-design")
    finally:
        if _old is not None:
            _os.environ["SUPER_AGENT_TEST"] = _old

    assert out1["ok"] is True and out2["ok"] is True
    log = _sp.run(["git", "-C", str(repo), "log", "design/my-design", "--oneline"],
                   capture_output=True, text=True).stdout
    assert "design and task DAG" in log
    # exactly one docs commit even though drive() ran twice (nothing new to commit the 2nd time)
    assert log.count("design and task DAG") == 1
    tracked = _sp.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "design/my-design"],
                       capture_output=True, text=True).stdout
    assert "docs/design/my-design.md" in tracked
    assert "docs/design/my-design_tasks/my-design.md" in tracked


def test_drive_push_flag_pushes_target_branch_once(tmp_path, monkeypatch) -> None:
    """--push (push=True) must push target_branch to origin once, after all
    tasks finish; push=False (default) must never call `git push`."""
    import os as _os
    import subprocess as _sp

    repo, design, task_file = _init_repo_with_design_and_tasks(tmp_path)
    remote = tmp_path / "remote.git"
    _sp.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _sp.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    monkeypatch.chdir(repo)
    _old = _os.environ.pop("SUPER_AGENT_TEST", None)
    try:
        with mock.patch.object(drive, "structural_check", return_value=[]), \
             mock.patch.object(drive, "implement", return_value={"ok": True, "commit": "c1"}), \
             mock.patch.object(drive, "run_pipeline", return_value={"verdict": "pass"}), \
             mock.patch.object(drive, "integrate", return_value={"ok": True, "commit": "c2"}), \
             mock.patch.object(drive, "create_worktree", return_value={"ok": True, "path": str(repo)}), \
             mock.patch.object(drive, "schedule"):
            out_no_push = drive.drive("", str(design), str(task_file), seq=None, dry_run=False,
                                       target_branch="design/my-design", push=False)
            out_pushed = drive.drive("", str(design), str(task_file), seq=None, dry_run=False,
                                      target_branch="design/my-design", push=True)
    finally:
        if _old is not None:
            _os.environ["SUPER_AGENT_TEST"] = _old

    assert out_no_push["ok"] is True and "push" not in out_no_push
    assert out_pushed["ok"] is True
    assert out_pushed["push"]["ok"] is True
    remote_log = _sp.run(["git", "--git-dir", str(remote), "log", "design/my-design", "--oneline"],
                          capture_output=True, text=True).stdout
    assert "design and task DAG" in remote_log
