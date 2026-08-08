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

# 状態の重み付き順位（進んだ方が強い）。詳細は dashboard-priority-design.md 要件 A。
# integrated > passed > implemented > failed > leased > scheduled > created > unknown
STATUS_RANK = {
    "integrated": 6,
    "passed": 5,
    "implemented": 4,
    "failed": 3,
    "leased": 2,
    "scheduled": 1,
    "created": 0,
    "unknown": -1,
}


def _rank_of(status: str) -> int:
    """Return the priority rank of a status string (higher == further along).

    Statuses absent from STATUS_RANK keep legacy behaviour: custom statuses and
    ``unknown`` sit just above the judgement-unavailable tier, while any
    ``judgment*`` value (``judgment``, ``judgment_unavailable``,
    ``judgment:<verdict>`` for a non PASS/FAIL verdict) is deliberately weaker
    than every concrete implementation state so it never overwrites a real
    progress status (requirement A).
    """
    if status in STATUS_RANK:
        return STATUS_RANK[status]
    if status.startswith("judgment"):
        return -2
    return -1


def _event_status(ev: dict[str, Any]) -> tuple[str | None, int]:
    """Resolve ``(status_string, rank)`` for a single event.

    Returns ``(None, ...)`` when the event carries no usable status info.
    """
    if "status" in ev and ev["status"]:
        status = str(ev["status"])
        return status, _rank_of(status)

    type_ = ev.get("type", "")
    if type_ == "judgment":
        verdict = ev.get("verdict", "")
        if verdict == "PASS":
            return "passed", STATUS_RANK["passed"]
        if verdict == "FAIL":
            return "failed", STATUS_RANK["failed"]
        if verdict:
            return f"judgment:{verdict}", _rank_of(f"judgment:{verdict}")
        return "judgment", _rank_of("judgment")

    if type_ in STATUS_MAP:
        status = STATUS_MAP[type_]
        return status, _rank_of(status)

    if type_:
        # Unknown event type: keep the raw type as the status (legacy compat).
        return type_, _rank_of(type_)

    return None, 0


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

    Each task's final status is the *furthest-advanced* state seen across all
    its events (requirement A: state-transition priority). A later event only
    overwrites when it represents a stronger/newer status, so weak states such
    as ``judgment_unavailable`` or ``judgment:*`` never clobber a concrete
    implementation status (``implemented`` / ``integrated`` etc.).

    Args:
        events: List of ledger event dicts.

    Returns:
        Dict mapping task_id to status string.
    """
    model: dict[str, str] = {}
    rank_of: dict[str, int] = {}
    if not events:
        return model

    for ev in events:
        if not isinstance(ev, dict):
            continue
        task_id = _task_id_of(ev)
        if not task_id:
            continue

        status, rank = _event_status(ev)
        if status is None:
            continue

        cur_rank = rank_of.get(task_id)
        if cur_rank is None or rank > cur_rank:
            model[task_id] = status
            rank_of[task_id] = rank

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
