#!/usr/bin/env python
"""Dashboard model builder and renderer.

Converts ledger events into a structured task status model, then renders that
model as Markdown or HTML.
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


def _task_id_of(ev: dict[str, Any]) -> str | None:
    """Extract the canonical task id from a ledger event.

    The canonical field is ``task_id``. For legacy / vendor-produced events
    that only carry ``event_id`` (``{task_id}:{seq}``), the id is derived by
    stripping the ``:<seq>`` suffix so we don't end up with redundant
    ``task-1:3`` keys in the dashboard model.
    """
    task_id = ev.get("task_id")
    if task_id:
        return str(task_id)
    event_id = ev.get("event_id")
    if event_id and ":" in event_id:
        return event_id.split(":", 1)[0]
    if event_id:
        return str(event_id)
    return None


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
        task_id = _task_id_of(ev)
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


def render_markdown(model: dict[str, str]) -> str:
    """Render the task status model as a Markdown table."""
    lines = ["# Dashboard", "", "| Task ID | Status |", "| --- | --- |"]
    for task_id, status in sorted(model.items()):
        lines.append(f"| {task_id} | {status} |")
    return "\n".join(lines) + "\n"


def render_html(model: dict[str, str]) -> str:
    """Render the task status model as an HTML table."""
    rows = ""
    for task_id, status in sorted(model.items()):
        rows += f"<tr><td>{task_id}</td><td>{status}</td></tr>\n"
    return (
        "<!DOCTYPE html>\n"
        "<html>\n<head><title>Dashboard</title></head>\n"
        "<body>\n<h1>Dashboard</h1>\n"
        "<table>\n<thead><tr><th>Task ID</th><th>Status</th></tr></thead>\n"
        f"<tbody>\n{rows}</tbody>\n</table>\n"
        "</body>\n</html>\n"
    )
