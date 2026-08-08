"""Tests for the planner role (adaptive re-planning during Stage B)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.roles import planner as planner_role


SAMPLE_TASKS = [
    {"task_id": "dashboard-model", "goal": "build_model と render_md を実装",
     "acceptance": [], "depends_on": [], "touch_allow": ["harness/roles/dashboard_model.py"]},
    {"task_id": "dashboard-render-md", "goal": "render_md を使って md 出力",
     "acceptance": [], "depends_on": ["dashboard-model"],
     "touch_allow": ["harness/roles/dashboard_md.py"]},
    {"task_id": "dashboard-render-html", "goal": "render_html を実装",
     "acceptance": [], "depends_on": ["dashboard-model"],
     "touch_allow": ["harness/roles/dashboard_html.py"]},
]


def test_replan_invokes_vendor_and_returns_revised_tasks() -> None:
    """replan() calls the planner vendor and returns a revised task list."""
    fake = {
        "ok": True,
        "tasks": [
            {"task_id": "dashboard-model", "goal": "g", "acceptance": [],
             "depends_on": [], "touch_allow": ["harness/roles/dashboard_model.py"]},
            {"task_id": "dashboard-render-md", "goal": "g", "acceptance": [],
             "depends_on": ["dashboard-model"],
             "touch_allow": ["harness/roles/dashboard_md.py"]},
        ],
        "investigation_needed": [],
        "notes": "kept two independent files",
    }
    with mock.patch.object(planner_role, "invoke", return_value=fake) as m_inv, \
         mock.patch.object(planner_role, "load_vendors", return_value={"claude": {}}):
        rep = planner_role.replan("dashboard", SAMPLE_TASKS, events=[],
                                  vendor="claude", seq=None, dry_run=False)
    assert rep["ok"] is True
    assert m_inv.called
    assert len(rep["tasks"]) == 2
    assert "notes" in rep


def test_replan_carves_out_investigation_tasks() -> None:
    """When the vendor reports investigation_needed, replan surfaces them."""
    fake = {
        "ok": True,
        "tasks": SAMPLE_TASKS,
        "investigation_needed": [
            {"task_id": "investigate-ledger-schema",
             "goal": "ledger の event スキーマを調査する"},
        ],
        "notes": "schema unknown, investigate first",
    }
    with mock.patch.object(planner_role, "invoke", return_value=fake), \
         mock.patch.object(planner_role, "load_vendors", return_value={"claude": {}}):
        rep = planner_role.replan("dashboard", SAMPLE_TASKS, events=[],
                                  vendor="claude", seq=None, dry_run=False)
    assert len(rep["investigation_needed"]) == 1
    assert rep["investigation_needed"][0]["task_id"] == "investigate-ledger-schema"


def test_replan_parses_string_output() -> None:
    """If the vendor returns a JSON string, replan parses it."""
    fake_str = ('{"ok": true, "tasks": [{"task_id": "T1", "goal": "g", '
                '"acceptance": []}], "investigation_needed": [], "notes": "ok"}')
    with mock.patch.object(planner_role, "invoke", return_value=fake_str), \
         mock.patch.object(planner_role, "load_vendors", return_value={"claude": {}}):
        rep = planner_role.replan("req", SAMPLE_TASKS, events=[],
                                  vendor="claude", seq=None, dry_run=False)
    assert rep["ok"] is True
    assert rep["tasks"][0]["task_id"] == "T1"


def test_replan_dry_run_does_not_call_vendor() -> None:
    with mock.patch.object(planner_role, "invoke") as m_inv:
        rep = planner_role.replan("req", SAMPLE_TASKS, events=[],
                                  vendor="claude", seq=None, dry_run=True)
    assert rep["dry_run"] is True
    assert not m_inv.called
    # returns the original tasks unchanged
    assert len(rep["tasks"]) == len(SAMPLE_TASKS)


def test_summarize_events_compact() -> None:
    events = [
        {"type": "integrated", "task_id": "dashboard-model"},
        {"type": "implementer.error", "task_id": "dashboard-render-md",
         "error": "import failed"},
    ]
    s = planner_role._summarize_events(events)
    assert "dashboard-model" in s
    assert "import failed" in s


def test_detect_oversplit_finds_shared_file() -> None:
    tasks = [
        {"task_id": "A", "touch_allow": ["harness/x.py"]},
        {"task_id": "B", "touch_allow": ["harness/x.py"]},
        {"task_id": "C", "touch_allow": ["harness/y.py"]},
    ]
    hint = planner_role._detect_oversplit(tasks)
    assert "harness/x.py" in hint
    assert "A" in hint and "B" in hint
    assert "C" not in hint.split("harness/x.py")[1] if "C" in hint else True


def test_detect_oversplit_none_when_disjoint() -> None:
    tasks = [
        {"task_id": "A", "touch_allow": ["harness/x.py"]},
        {"task_id": "B", "touch_allow": ["harness/y.py"]},
    ]
    hint = planner_role._detect_oversplit(tasks)
    assert "なし" in hint


def test_merge_oversplit_combines_shared_file_tasks() -> None:
    tasks = [
        {"task_id": "dashboard-model", "goal": "model",
         "acceptance": [], "depends_on": [], "touch_allow": ["harness/roles/dashboard.py"]},
        {"task_id": "dashboard-render-md", "goal": "md",
         "acceptance": [], "depends_on": ["dashboard-model"],
         "touch_allow": ["harness/roles/dashboard.py"]},
        {"task_id": "other-task", "goal": "o",
         "acceptance": [], "depends_on": [], "touch_allow": ["harness/other.py"]},
    ]
    merged, notes = planner_role._merge_oversplit(tasks)
    ids = [t["task_id"] for t in merged]
    assert "other-task" in ids
    # dashboard-model + dashboard-render-md merged into one
    assert len([t for t in merged if t["task_id"].startswith("dashboard")]) == 1
    assert any("過分割をマージ" in n for n in notes)
    merged_dash = [t for t in merged if t["task_id"].startswith("dashboard")][0]
    assert "harness/roles/dashboard.py" in merged_dash["touch_allow"]
    # intra-group dep dropped, external deps kept
    assert "dashboard-model" not in merged_dash["depends_on"]
