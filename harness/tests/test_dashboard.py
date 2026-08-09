#!/usr/bin/env python
"""Tests for dashboard role and build_model."""
from __future__ import annotations

from harness.roles.dashboard import (
    build_model,
    progress_summary,
    render_html,
    render_markdown,
)


def test_build_model() -> None:
    events = [
        {"task_id": "task-1", "type": "task.created"},
        {"task_id": "task-1", "type": "task.scheduled"},
        {"task_id": "task-1", "type": "task.leased"},
        {"task_id": "task-2", "type": "task.created"},
        {"task_id": "task-1", "type": "task.implemented"},
        {"task_id": "task-1", "type": "review.pass"},
        {"task_id": "task-1", "type": "integrate.ok"},
        {"task_id": "task-2", "type": "task.leased"},
        {"task_id": "task-2", "type": "implementer.error", "error": "failed build"},
        {"task_id": "task-3", "type": "judgment", "verdict": "PASS"},
        {"task_id": "task-4", "type": "judgment", "verdict": "FAIL"},
        {"task_id": "task-5", "status": "custom_status"},
    ]

    model = build_model(events)

    assert isinstance(model, dict)
    assert model.get("task-1") == "integrated"
    assert model.get("task-2") == "failed"
    assert model.get("task-3") == "passed"
    assert model.get("task-4") == "failed"
    assert model.get("task-5") == "custom_status"


def test_build_model_empty() -> None:
    assert build_model([]) == {}


def test_build_model_uses_real_ledger_event_id_fallback() -> None:
    """Live ledger events carry `event_id` of form `{task_id}:{seq}` and a
    `task_id` field. The model must key by the canonical task id, not the
    redundant `task-1:3` string."""
    events = [
        {"event_id": "task-1:1", "task_id": "task-1", "seq": 1, "type": "task.created"},
        {"event_id": "task-1:5", "task_id": "task-1", "seq": 5, "type": "integrate.ok"},
        {"event_id": "task-2:2", "task_id": "task-2", "seq": 2,
         "type": "implementer.error", "error": "boom"},
    ]
    model = build_model(events)
    assert set(model) == {"task-1", "task-2"}
    assert model["task-1"] == "integrated"
    assert model["task-2"] == "failed"


def test_build_model_legacy_event_id_only() -> None:
    """Some vendor-produced events only carry `event_id` (no `task_id`).
    The id should be derived by stripping the `:<seq>` suffix."""
    events = [
        {"event_id": "legacy-9:7", "seq": 7, "type": "task.created"},
    ]
    model = build_model(events)
    assert model == {"legacy-9": "created"}


def test_render_markdown_table() -> None:
    model = {"task-1": "integrated", "task-2": "failed"}
    md = render_markdown(model)
    assert md.startswith("# Dashboard")
    assert "| Task ID | Status |" in md
    assert "| task-1 | integrated |" in md
    assert "| task-2 | failed |" in md


def test_render_html_table() -> None:
    model = {"task-1": "integrated", "task-2": "failed"}
    html = render_html(model)
    assert "<!DOCTYPE html>" in html
    assert "<th>Task ID</th><th>Status</th>" in html
    assert "<tr><td>task-1</td><td>" in html


def test_build_model_priority_stronger_state_wins() -> None:
    """Requirement A: a stronger (further-along) state must win over a weaker
    one even when the weaker event appears later in the stream."""
    events = [
        {"task_id": "T1", "type": "task.implemented"},
        {"task_id": "T1", "type": "integrated"},
        # a late, weak judgment_unavailable must NOT clobber `integrated`
        {"task_id": "T1", "type": "judgment", "verdict": "unavailable"},
    ]
    model = build_model(events)
    assert model["T1"] == "integrated"


def test_build_model_priority_keeps_weak_judgment() -> None:
    """When no concrete progress state exists, a weak judgment event is
    preserved (it is weaker than even `created`)."""
    events = [
        {"task_id": "T2", "type": "judgment", "verdict": "unavailable"},
    ]
    model = build_model(events)
    assert model["T2"] == "judgment:unavailable"


def test_build_model_priority_equal_rank_latest_wins() -> None:
    """Same-rank states fall back to latest (chronological) event."""
    events = [
        {"task_id": "T3", "type": "task.implemented"},
        {"task_id": "T3", "type": "task.implemented"},
    ]
    model = build_model(events)
    assert model["T3"] == "implemented"


def test_build_model_aggregates_speculative_subchannel() -> None:
    """Requirement B: speculative sub-channels (PA__hermes_0) are aggregated
    into their parent logical task (PA)."""
    events = [
        {"task_id": "PA", "type": "task.created"},
        {"task_id": "PA__hermes_0", "type": "task.implemented"},
        {"task_id": "PA__hermes_1", "type": "review.pass"},
    ]
    model = build_model(events)
    assert set(model) == {"PA"}
    # strongest status across parent + sub-channels wins
    assert model["PA"] == "passed"


def test_build_model_transient_event_does_not_pin_status() -> None:
    """Requirement B: a transient event (verification.run) must not pin the
    task's status; a later terminal event overrides it."""
    events = [
        {"task_id": "T4", "type": "verification.run"},
        {"task_id": "T4", "type": "review.pass"},
    ]
    model = build_model(events)
    assert model["T4"] == "passed"


def test_render_markdown_summary() -> None:
    """Markdown output must include a progress-summary section with totals,
    completed count, rate and per-status distribution."""
    model = {"task-1": "integrated", "task-2": "failed", "task-3": "passed"}
    md = render_markdown(model)
    assert "## Progress Summary" in md
    assert "Total logical tasks: 3" in md
    assert "Completed (integrated/passed): 2 (66.7%)" in md
    assert "By status:" in md
    # the tasks table is still present
    assert "| task-1 | integrated |" in md


def test_render_html_dark_mode_and_badges() -> None:
    """HTML output must be dark-themed and render colour-coded status badges
    plus a progress-summary card."""
    model = {"task-1": "integrated", "task-2": "failed", "task-3": "passed"}
    out = render_html(model)
    # dark-mode styling present
    assert "color-scheme: dark" in out
    assert "background:#0f172a" in out
    # progress summary card
    assert "Progress Summary" in out
    assert "Logical Tasks" in out
    assert "66.7%" in out
    # colour badges for each status
    assert 'class="badge badge-green"' in out
    assert 'class="badge badge-red"' in out
    # a concrete badge label
    assert ">Integrated<" in out
    assert ">Failed<" in out


def test_progress_summary_empty() -> None:
    s = progress_summary({})
    assert s["total"] == 0
    assert s["done"] == 0
    assert s["rate"] == 0.0
    assert s["counts"] == {}


def test_progress_summary_counts() -> None:
    model = {"a": "integrated", "b": "passed", "c": "failed", "d": "scheduled"}
    s = progress_summary(model)
    assert s["total"] == 4
    assert s["done"] == 2
    assert s["rate"] == 50.0
    assert s["counts"] == {
        "integrated": 1, "passed": 1, "failed": 1, "scheduled": 1
    }
