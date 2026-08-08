#!/usr/bin/env python
"""Dashboard model builder and renderer.

Converts ledger events into a structured task status model.
"""
from __future__ import annotations

from typing import Any

STATUS_MAP = {
    "integrated": "integrated",
    "integrate.ok": "integrated",
    "review.pass": "passed",
    "task.implemented": "implemented",
    "implement.ok": "implemented",
    "artifact.produced": "implemented",
    "task.leased": "leased",
    "task.scheduled": "scheduled",
    "task.created": "created",
    "review.fail": "failed",
    "review.failed": "failed",
    "implementer.error": "failed",
    "worktree.error": "failed",
    "integration.failed": "failed",
    "integrated.failed": "failed",
    "integrate.error": "failed",
    "conflict": "failed",
    "agent.error": "failed",
}


def build_model(events: list[dict[str, Any]]) -> dict[str, str]:
    """Convert ledger events into a structured model mapping task_id -> status.

    Args:
        events: List of ledger event dicts.

    Returns:
        Dict mapping task_id to status string.
    """
    model: dict[str, str] = {}
    if not events:
        return model

    for ev in events:
        if not isinstance(ev, dict):
            continue
        task_id = ev.get("task_id")
        if not task_id:
            continue

        if "status" in ev and ev["status"]:
            model[task_id] = str(ev["status"])
            continue

        type_ = ev.get("type", "")
        if type_ == "judgment":
            verdict = ev.get("verdict", "")
            if verdict == "PASS":
                model[task_id] = "passed"
            elif verdict == "FAIL":
                model[task_id] = "failed"
            elif verdict:
                model[task_id] = f"judgment:{verdict}"
            else:
                model[task_id] = "judgment"
        elif type_ in STATUS_MAP:
            model[task_id] = STATUS_MAP[type_]
        elif type_:
            if task_id not in model:
                model[task_id] = type_

    return model
