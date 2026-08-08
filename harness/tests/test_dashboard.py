#!/usr/bin/env python
"""Tests for dashboard role and build_model."""
from __future__ import annotations

from harness.roles.dashboard import (
    build_model,
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
    assert "<tr><td>task-1</td><td>integrated</td></tr>" in html
    assert "<tr><td>task-2</td><td>failed</td></tr>" in html

