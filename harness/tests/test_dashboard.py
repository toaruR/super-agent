#!/usr/bin/env python
"""Tests for dashboard role and build_model."""
from __future__ import annotations

from harness.roles.dashboard import build_model


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
