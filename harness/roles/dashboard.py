#!/usr/bin/env python
"""Dashboard model builder and renderer.

Converts ledger events into a structured task status model, then renders that
model as Markdown or HTML.

Improvements (dashboard-improvement design):
  * Logical-task aggregation: speculative sub-channels such as ``PA__hermes_0``
    are folded into their parent logical task (``PA``) so the dashboard reports
    one row per logical task instead of one row per sub-channel.
  * A progress summary (total / completed / rate / distribution).
  * Dark-mode HTML with colour-coded status badges + a summary card.
  * Markdown output gains a progress-summary section while still emitting the
    ``| task-id | status |`` table (kept for CLI compatibility).

Awareness improvements (dashboard-awareness design):
  * Stale detection: non-terminal tasks (``created`` / ``scheduled`` /
    ``leased`` / ``implemented``) whose ``updated_at`` is older than a
    threshold (default 30 minutes) are flagged ``is_stale`` so a stuck task is
    not shown with the same badge as a healthy in-progress one.
  * Failure reason: when a task settles on ``failed``, the reason string of the
    causing event (``error`` → ``reason`` → ``why``) is kept on the model and
    surfaced by both renderers.
  * A colour-coded progress bar in the HTML summary card.
"""
from __future__ import annotations

import html
import time
from datetime import datetime
from typing import Any

from harness.core.progress import load_all_progress

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

# Statuses that count as "done" for the progress-rate metric. Kept consistent
# with harness/cli.py::_DONE_STATUSES.
_DONE_STATUSES = ("integrated", "passed")

# Statuses that are still "in flight": a task sitting in one of these has work
# outstanding, so it is a candidate for stale detection. Every other status
# (integrated / passed / failed / judgment:* / custom) is treated as terminal
# and is never flagged stale.
_NON_TERMINAL_STATUSES = ("created", "scheduled", "leased", "implemented")

# Default stale threshold: a non-terminal task untouched for this long (in
# seconds) is considered stuck. Overridable per call via build_model(...,
# stale_after=...); deliberately not config-file driven yet.
_DEFAULT_STALE_AFTER = 30 * 60

# Event fields consulted, in order, when extracting a failure reason.
_REASON_FIELDS = ("error", "reason", "why")

# Transient / lifecycle events that must NOT pin a task's status (requirement
# B: "過渡イベントで状態が止まらず"). They are informational only, so the
# final status is derived from a terminal event instead of a raw transient type.
_TRANSIENT_TYPES = {
    "verification.run",
    "verification.start",
    "verification.pending",
    "task.running",
    "task.start",
    "task.pending",
    "agent.running",
    "agent.start",
}

# Human-readable label + CSS class for each canonical status (requirement C).
# Colours follow the design: Integrated/Passed=green, In Progress=blue,
# Failed=red, Scheduled/others=gray.
_STATUS_BADGE = {
    "integrated": ("Integrated", "badge-green"),
    "passed": ("Passed", "badge-green"),
    "implemented": ("Implemented", "badge-blue"),
    "leased": ("Leased", "badge-blue"),
    "scheduled": ("Scheduled", "badge-gray"),
    "created": ("Created", "badge-gray"),
    "failed": ("Failed", "badge-red"),
    "unknown": ("Unknown", "badge-gray"),
}

# Colour buckets used by the HTML progress bar. A stale task is pulled out of
# its status bucket and shown in the dedicated orange one.
_BAR_BUCKETS = (
    ("Completed", "bar-green", ("integrated", "passed")),
    ("In Progress", "bar-blue", ("implemented", "leased")),
    ("Stale", "bar-orange", ()),
    ("Failed", "bar-red", ("failed",)),
    ("Pending", "bar-gray", ("scheduled", "created")),
)

# Maximum number of characters of a failure reason rendered inline; the full
# text stays available through the ``title`` attribute.
_REASON_MAX_LEN = 80


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


def format_ts(ts: Any) -> str:
    """Format a unix-epoch timestamp (as stored by harness.core.ledger) for
    display. Returns "-" for missing/unparseable values."""
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def load_progress(ledger_path: str) -> dict[str, dict[str, Any]]:
    """Load the progress side-channel (docs/design/timeout-liveness-watchdog.md
    §0) for a ledger, keyed by task_id. Thin wrapper so callers only need to
    import from harness.roles.dashboard."""
    return load_all_progress(ledger_path)


def _relative_time(ts: Any, now_ts: float) -> str:
    """Render a unix-epoch timestamp as a short "N ago" string relative to
    `now_ts`. Returns "-" for missing/unparseable values."""
    if not ts:
        return "-"
    try:
        delta = now_ts - float(ts)
    except (TypeError, ValueError):
        return "-"
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _badge(status: str) -> tuple[str, str]:
    """Return ``(label, css_class)`` for rendering a status badge.

    Unknown / custom statuses fall back to a gray badge labelled with the raw
    status string.
    """
    if status in _STATUS_BADGE:
        return _STATUS_BADGE[status]
    return status, "badge-gray"


def _task_badge(info: dict[str, Any]) -> tuple[str, str]:
    """Return ``(label, css_class)`` for a task entry's badge.

    A stale task keeps its status label but is rendered in the dedicated
    orange class so it is visually distinct from a healthy in-progress task.
    """
    label, cls = _badge(info["status"])
    if info.get("is_stale"):
        return f"{label} (Stale)", "badge-orange"
    return label, cls


def _event_reason(ev: dict[str, Any]) -> str:
    """Extract a failure reason from an event.

    ``error`` → ``reason`` → ``why``: the first field that carries a truthy
    value wins. Returns ``""`` when the event carries none of them (callers
    must not invent a placeholder).
    """
    for field in _REASON_FIELDS:
        value = ev.get(field)
        if value:
            return str(value)
    return ""


def _is_stale(
    status: str | None,
    updated_at: Any,
    now_ts: float,
    stale_after: float,
) -> bool:
    """Return True when a non-terminal task has been untouched for too long.

    Only ``created`` / ``scheduled`` / ``leased`` / ``implemented`` qualify;
    terminal statuses (``integrated`` / ``passed`` / ``failed`` / custom) are
    never stale. Tasks without a usable ``updated_at`` are not stale either —
    we cannot prove they are stuck.
    """
    if status not in _NON_TERMINAL_STATUSES:
        return False
    if not updated_at:
        return False
    try:
        updated = float(updated_at)
    except (TypeError, ValueError):
        return False
    return (now_ts - updated) > stale_after


def _event_status(ev: dict[str, Any]) -> tuple[str | None, int]:
    """Resolve ``(status_string, rank)`` for a single event.

    Returns ``(None, ...)`` when the event carries no usable status info (or is
    a transient event that must not pin status).
    """
    if "status" in ev and ev["status"]:
        status = str(ev["status"])
        return status, _rank_of(status)

    type_ = ev.get("type", "")

    if type_ in _TRANSIENT_TYPES:
        # Informational only — never freeze the task at a transient raw type.
        return None, 0

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


def _logical_parent(task_id: str) -> str:
    """Map a speculative sub-channel id to its parent logical task id.

    Sub-channels are named ``<parent>__<channel>`` (e.g. ``PA__hermes_0``); the
    parent logical task id is the portion before the first ``__``. Ids without
    ``__`` are returned unchanged.
    """
    parts = task_id.split("__", 1)
    if len(parts) == 2 and parts[1]:
        return parts[0]
    return task_id


def _empty_task_entry() -> dict[str, Any]:
    return {
        "status": None, "_rank": None,
        "design_file": "", "task_file": "",
        "created_at": "", "updated_at": "",
        "is_stale": False, "reason": "",
        "last_activity_ts": "", "activity_detail": "",
    }


def _merge_event_meta(entry: dict[str, Any], ev: dict[str, Any]) -> None:
    """Fold one event's design_file/task_file/ts into a task entry.

    design_file/task_file are taken from the first event that carries them
    (they should be constant across a task's events). created_at/updated_at
    track the min/max ``ts`` seen so far.
    """
    df = ev.get("design_file") or ""
    tf = ev.get("task_file") or ""
    ts = ev.get("ts") or ""
    if df and not entry["design_file"]:
        entry["design_file"] = df
    if tf and not entry["task_file"]:
        entry["task_file"] = tf
    if ts:
        if not entry["created_at"] or ts < entry["created_at"]:
            entry["created_at"] = ts
        if not entry["updated_at"] or ts > entry["updated_at"]:
            entry["updated_at"] = ts


def _merge_meta(agg: dict[str, Any], entry: dict[str, Any]) -> None:
    """Merge a raw task entry's meta fields into its parent's aggregate entry."""
    if entry["design_file"] and not agg["design_file"]:
        agg["design_file"] = entry["design_file"]
    if entry["task_file"] and not agg["task_file"]:
        agg["task_file"] = entry["task_file"]
    ca = entry["created_at"]
    if ca and (not agg["created_at"] or ca < agg["created_at"]):
        agg["created_at"] = ca
    ua = entry["updated_at"]
    if ua and (not agg["updated_at"] or ua > agg["updated_at"]):
        agg["updated_at"] = ua
    lat = entry.get("last_activity_ts")
    if lat and (not agg.get("last_activity_ts") or lat > agg["last_activity_ts"]):
        agg["last_activity_ts"] = lat
        agg["activity_detail"] = entry.get("activity_detail", "")


def build_model(
    events: list[dict[str, Any]],
    now: float | None = None,
    stale_after: float = _DEFAULT_STALE_AFTER,
    progress: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Convert ledger events into a structured model mapping task_id -> info.

    Each info dict has keys: ``status``, ``design_file``, ``task_file``,
    ``created_at``, ``updated_at`` (the latter two are unix-epoch seconds, or
    ``""`` when no event carried a ``ts``), ``is_stale``, ``reason``,
    ``last_activity_ts``/``activity_detail`` (from the progress side-channel,
    see ``load_progress()``) and ``last_activity_display`` (a precomputed
    "N ago" string relative to ``now``).

    ``progress``, if given, is a task_id -> progress-file dict as returned by
    ``load_progress()`` (docs/design/timeout-liveness-watchdog.md §0/§4). Its
    ``last_activity_ts`` feeds stale detection alongside ``updated_at`` — a
    long-running single implement call whose ledger ``updated_at`` is pinned
    at its first ``task.leased`` event is no longer misreported as stale as
    long as it keeps heartbeating.

    Two passes:

    1. Per raw task id, the final status is the *furthest-advanced* state seen
       across all its events (requirement A: state-transition priority). A later
       event only overwrites when it represents a stronger/newer status, so weak
       states such as ``judgment_unavailable`` or ``judgment:*`` never clobber a
       concrete implementation status (``implemented`` / ``integrated`` etc.).
       Transient events (``verification.run`` …) are ignored for status.
       design_file/task_file/created_at/updated_at are folded in regardless of
       whether the event carries a status.

    2. Speculative sub-channels (``PA__hermes_0``) are aggregated under their
       parent logical task (``PA``); the parent's status is the strongest status
       seen across itself and all of its sub-channels (requirement B), and its
       created_at/updated_at span the earliest/latest across itself and all
       sub-channels.

    Finally each logical task is annotated with:

    * ``is_stale``: True when the task sits in a non-terminal status
      (``created`` / ``scheduled`` / ``leased`` / ``implemented``) and its
      ``updated_at`` is older than ``stale_after`` seconds relative to ``now``.
      Terminal statuses (``integrated`` / ``passed`` / ``failed`` / custom) are
      never stale.
    * ``reason``: for tasks that settled on ``failed``, the reason string of the
      causing event (``error`` → ``reason`` → ``why``, first match wins). Left
      as ``""`` when unavailable or when the task did not fail — never padded
      with a placeholder.

    Args:
        events: List of ledger event dicts.
        now: Reference timestamp (unix epoch seconds) for stale detection.
            Defaults to the current wall clock; tests inject a fixed value.
        stale_after: Stale threshold in seconds (default 30 minutes).

    Returns:
        Dict mapping logical task_id to an info dict.
    """
    if not events:
        return {}

    now_ts = time.time() if now is None else float(now)

    # Pass 1: raw task id -> strongest status + meta.
    raw: dict[str, dict[str, Any]] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        task_id = _task_id_of(ev)
        if not task_id:
            continue

        entry = raw.setdefault(task_id, _empty_task_entry())
        _merge_event_meta(entry, ev)
        if progress and task_id in progress and not entry["last_activity_ts"]:
            p = progress[task_id]
            lat = p.get("last_activity_ts")
            if lat:
                entry["last_activity_ts"] = lat
                entry["activity_detail"] = p.get("detail", "")

        status, rank = _event_status(ev)
        if status is None:
            continue
        if entry["_rank"] is None or rank > entry["_rank"]:
            entry["status"] = status
            entry["_rank"] = rank
            if status == "failed":
                entry["reason"] = _event_reason(ev)
        elif status == "failed" and not entry["reason"]:
            # Same-rank failure event: keep the first reason we can find so a
            # bare `review.fail` doesn't mask a later, detailed error event.
            entry["reason"] = _event_reason(ev)

    for entry in raw.values():
        if entry["status"] != "failed":
            entry["reason"] = ""

    # Pass 2: aggregate speculative sub-channels into parent logical tasks.
    aggregated: dict[str, dict[str, Any]] = {}
    for task_id, entry in raw.items():
        if entry["status"] is None:
            continue
        parent = _logical_parent(task_id)
        agg = aggregated.get(parent)
        if agg is None:
            agg = _empty_task_entry()
            aggregated[parent] = agg
        if agg["_rank"] is None or entry["_rank"] > agg["_rank"]:
            agg["status"] = entry["status"]
            agg["_rank"] = entry["_rank"]
            agg["reason"] = entry["reason"]
        elif entry["status"] == agg["status"] and entry["reason"] and not agg["reason"]:
            agg["reason"] = entry["reason"]
        _merge_meta(agg, entry)

    for entry in aggregated.values():
        entry.pop("_rank", None)
        if entry["status"] != "failed":
            entry["reason"] = ""
        lat = entry.get("last_activity_ts")
        effective_updated = entry["updated_at"]
        if lat and (not effective_updated or lat > effective_updated):
            effective_updated = lat
        entry["is_stale"] = _is_stale(
            entry["status"], effective_updated, now_ts, stale_after
        )
        entry["last_activity_display"] = _relative_time(
            lat or entry["updated_at"], now_ts
        )
    return aggregated


# Heading used for tasks whose design_file couldn't be determined.
_UNKNOWN_DESIGN_FILE = "(design file unknown)"


def group_by_design_file(
    model: dict[str, dict[str, Any]]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Group a task model by design_file for grouped display in status/dashboard.

    Returns an ordered dict of design_file -> {task_id: info}, sorted by
    design_file with tasks lacking a design_file (grouped under
    ``_UNKNOWN_DESIGN_FILE``) sorted last.
    """
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for task_id, info in model.items():
        key = info.get("design_file") or _UNKNOWN_DESIGN_FILE
        groups.setdefault(key, {})[task_id] = info
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] == _UNKNOWN_DESIGN_FILE, kv[0])))


def progress_summary(model: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute an aggregate progress summary for the model.

    Returns a dict with keys: ``total`` (logical task count), ``done`` (count of
    integrated/passed), ``rate`` (done/total as a percentage, 0.0 when empty),
    ``counts`` (status -> count) and ``stale`` (count of tasks flagged
    ``is_stale``).
    """
    total = len(model)
    counts: dict[str, int] = {}
    stale = 0
    for info in model.values():
        status = info["status"]
        counts[status] = counts.get(status, 0) + 1
        if info.get("is_stale"):
            stale += 1
    done = sum(c for s, c in counts.items() if s in _DONE_STATUSES)
    rate = (done / total * 100.0) if total else 0.0
    return {"total": total, "done": done, "rate": rate,
            "counts": counts, "stale": stale}


def progress_bar_segments(
    model: dict[str, dict[str, Any]]
) -> list[tuple[str, str, int, float]]:
    """Build the segments of the HTML progress bar.

    Returns a list of ``(label, css_class, count, percent)`` tuples ordered as
    completed → in progress → stale → failed → pending. Stale tasks are counted
    only in the stale (orange) segment, never in their status bucket, so the
    percentages always add up to 100. Empty segments are omitted.
    """
    total = len(model)
    if not total:
        return []

    stale = sum(1 for info in model.values() if info.get("is_stale"))
    counts: dict[str, int] = {}
    for info in model.values():
        if info.get("is_stale"):
            continue
        status = info["status"]
        counts[status] = counts.get(status, 0) + 1

    segments: list[tuple[str, str, int, float]] = []
    for label, cls, statuses in _BAR_BUCKETS:
        count = stale if cls == "bar-orange" else sum(
            counts.get(s, 0) for s in statuses)
        if count:
            segments.append((label, cls, count, count / total * 100.0))

    # Anything not covered by a bucket (custom / judgment:* statuses) is shown
    # in the gray "Other" tail so the bar still totals 100%.
    bucketed = sum(seg[2] for seg in segments)
    if bucketed < total:
        other = total - bucketed
        segments.append(("Other", "bar-gray", other, other / total * 100.0))
    return segments


def _shorten(text: str, limit: int = _REASON_MAX_LEN) -> str:
    """Truncate ``text`` to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _md_status_cell(info: dict[str, Any]) -> str:
    """Status cell text for the Markdown table.

    Stale rows keep the raw status (CLI consumers grep for ``| id | status |``)
    and gain a ``⚠ stale`` marker so they are distinguishable from healthy
    in-progress rows.
    """
    status = html.escape(info["status"])
    if info.get("is_stale"):
        return f"{status} ⚠ stale"
    return status


def _md_cell(text: str) -> str:
    """Escape a free-form string for safe inclusion in a Markdown table cell."""
    return html.escape(text).replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(model: dict[str, dict[str, Any]]) -> str:
    """Render the task status model as Markdown.

    Emits a progress-summary section followed by the task list grouped into
    one ``| Task ID | Status | Created At | Updated At | Reason |`` table per
    design_file (the ``### <design_file>`` heading carries the grouping, so
    the table itself no longer repeats it per row). The
    ``| Task ID | Status |`` prefix of each row is kept for CLI compatibility
    with older consumers of this table.

    Stale rows are marked ``⚠ stale`` in the status column, and failed rows
    carry the failure reason in the trailing ``Reason`` column (left blank when
    no reason is available). No visual progress bar is emitted here — the
    textual distribution is enough for Markdown (design non-goal).
    """
    summary = progress_summary(model)
    lines = ["# Dashboard", ""]

    if model:
        lines.append("## Progress Summary")
        lines.append("")
        lines.append(f"- Total logical tasks: {summary['total']}")
        lines.append(
            f"- Completed (integrated/passed): {summary['done']} "
            f"({summary['rate']:.1f}%)"
        )
        dist = ", ".join(
            f"{k}={v}"
            for k, v in sorted(summary["counts"].items(),
                               key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(f"- By status: {dist}")
        if summary["stale"]:
            lines.append(f"- Stale (no progress for a while): {summary['stale']}")
        lines.append("")

    lines.append("## Tasks")
    lines.append("")
    if model:
        for design_file, tasks in group_by_design_file(model).items():
            lines.append(f"### {html.escape(design_file)}")
            lines.append("")
            lines.append("| Task ID | Status | Created At | Updated At | Reason | Last Activity |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for task_id, info in sorted(tasks.items()):
                lines.append(
                    f"| {html.escape(task_id)} | {_md_status_cell(info)} | "
                    f"{html.escape(format_ts(info['created_at']))} | "
                    f"{html.escape(format_ts(info['updated_at']))} | "
                    f"{_md_cell(info.get('reason') or '')} | "
                    f"{html.escape(info.get('last_activity_display') or '-')} |"
                )
            lines.append("")
    else:
        lines.append("(no logical tasks recorded)")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# Dark-mode stylesheet for the HTML renderer.
_HTML_CSS = """
  :root { color-scheme: dark; }
  body { background:#0f172a; color:#e2e8f0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin:0; padding:2rem; }
  h1 { font-size:1.5rem; margin:0 0 1.25rem; }
  h2 { font-size:1.1rem; margin:0 0 .75rem; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:12px; padding:1.25rem 1.5rem; margin-bottom:1.5rem; }
  .summary-grid { display:flex; gap:2rem; flex-wrap:wrap; }
  .metric { min-width:120px; }
  .metric .num { font-size:1.9rem; font-weight:700; line-height:1.1; }
  .metric .lbl { color:#94a3b8; font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; margin-top:.25rem; }
  .dist { margin-top:1rem; display:flex; gap:.5rem; flex-wrap:wrap; }
  table { width:100%; border-collapse:collapse; background:#1e293b; border:1px solid #334155; border-radius:12px; overflow:hidden; }
  th, td { text-align:left; padding:.65rem .9rem; border-bottom:1px solid #334155; }
  th { color:#94a3b8; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; }
  tr:last-child td { border-bottom:none; }
  .badge { display:inline-block; padding:.2rem .65rem; border-radius:999px; font-size:.78rem; font-weight:600; }
  .badge-green { background:rgba(34,197,94,.18); color:#4ade80; }
  .badge-blue  { background:rgba(59,130,246,.18); color:#60a5fa; }
  .badge-red   { background:rgba(239,68,68,.18);  color:#f87171; }
  .badge-gray  { background:rgba(148,163,184,.18); color:#cbd5e1; }
  .badge-orange { background:rgba(249,115,22,.20); color:#fb923c; }
  .progress-bar { display:flex; width:100%; height:14px; border-radius:999px; overflow:hidden; background:#0f172a; margin-top:1rem; }
  .progress-bar .seg { height:100%; }
  .bar-green  { background:#22c55e; }
  .bar-blue   { background:#3b82f6; }
  .bar-orange { background:#f97316; }
  .bar-red    { background:#ef4444; }
  .bar-gray   { background:#64748b; }
  .bar-legend { display:flex; gap:1rem; flex-wrap:wrap; margin-top:.55rem; color:#94a3b8; font-size:.72rem; }
  .bar-legend .key { display:inline-block; width:.6rem; height:.6rem; border-radius:2px; margin-right:.35rem; }
  .reason { display:block; margin-top:.3rem; color:#fca5a5; font-size:.72rem; line-height:1.3; max-width:34rem; }
"""


def _reason_html(info: dict[str, Any]) -> str:
    """Render the failure reason under a Failed badge.

    Returns ``""`` when the task carries no reason — nothing is padded in.
    Long reasons are truncated inline and kept in full in ``title``.
    """
    reason = (info.get("reason") or "").strip()
    if not reason:
        return ""
    return (
        f'<span class="reason" title="{html.escape(reason, quote=True)}">'
        f'{html.escape(_shorten(reason))}</span>'
    )


def _progress_bar_html(model: dict[str, dict[str, Any]]) -> str:
    """Render the colour-coded progress bar plus its legend.

    Returns ``""`` for an empty model so the summary card degrades to the
    existing numbers-only layout.
    """
    segments = progress_bar_segments(model)
    if not segments:
        return ""
    bar = "".join(
        f'<span class="seg {cls}" style="width:{pct:.1f}%" '
        f'title="{html.escape(label, quote=True)}: {count} ({pct:.1f}%)"></span>'
        for label, cls, count, pct in segments
    )
    legend = "".join(
        f'<span><span class="key {cls}"></span>{html.escape(label)} {count}</span>'
        for label, cls, count, _pct in segments
    )
    return (
        f'<div class="progress-bar">{bar}</div>\n'
        f'<div class="bar-legend">{legend}</div>\n'
    )


def render_html(
    model: dict[str, dict[str, Any]],
    refresh_interval: int | None = None,
) -> str:
    """Render the task status model as a dark-mode HTML dashboard.

    Includes a progress-summary card (total / completed / rate / distribution
    plus a colour-coded progress bar) and a table of tasks with colour-coded
    status badges — orange for stale tasks — the failure reason under a Failed
    badge, and design file / created / updated timestamps.

    ``refresh_interval``, if given, adds a ``<meta http-equiv="refresh">`` tag
    so a browser viewing the saved file auto-reloads every N seconds — meant
    for use with ``harness dashboard --watch`` (docs/design/
    timeout-liveness-watchdog.md §5), which is what keeps the file itself
    up to date. Omitted (``None``) by default: a static render has nothing
    to poll for.
    """
    summary = progress_summary(model)
    refresh_meta = (
        f'<meta http-equiv="refresh" content="{int(refresh_interval)}">\n'
        if refresh_interval else ""
    )

    tables = ""
    for design_file, tasks in group_by_design_file(model).items():
        rows = ""
        for task_id, info in sorted(tasks.items()):
            label, cls = _task_badge(info)
            rows += (
                f'<tr><td>{html.escape(task_id)}</td>'
                f'<td><span class="badge {cls}">{html.escape(label)}</span>'
                f'{_reason_html(info)}</td>'
                f'<td>{html.escape(format_ts(info["created_at"]))}</td>'
                f'<td>{html.escape(format_ts(info["updated_at"]))}</td>'
                f'<td>{html.escape(info.get("last_activity_display") or "-")}</td></tr>\n'
            )
        tables += (
            f'<h3>{html.escape(design_file)}</h3>\n'
            "<table>\n"
            "<thead><tr><th>Task ID</th><th>Status</th>"
            "<th>Created At</th><th>Updated At</th><th>Last Activity</th></tr></thead>\n"
            f"<tbody>\n{rows}</tbody>\n"
            "</table>\n"
        )

    dist = "".join(
        f'<span class="badge {_badge(k)[1]}">{html.escape(k)}={v}</span>'
        for k, v in sorted(summary["counts"].items(),
                           key=lambda kv: (-kv[1], kv[0]))
    )
    if summary["stale"]:
        dist += (
            f'<span class="badge badge-orange">stale={summary["stale"]}</span>'
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{refresh_meta}"
        "<title>Dashboard</title>\n"
        f"<style>{_HTML_CSS}\n</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Dashboard</h1>\n"
        '<div class="card">\n'
        "<h2>Progress Summary</h2>\n"
        '<div class="summary-grid">\n'
        f'<div class="metric"><div class="num">{summary["total"]}</div>'
        '<div class="lbl">Logical Tasks</div></div>\n'
        f'<div class="metric"><div class="num">{summary["done"]}</div>'
        '<div class="lbl">Completed</div></div>\n'
        f'<div class="metric"><div class="num">{summary["rate"]:.1f}%</div>'
        '<div class="lbl">Progress</div></div>\n'
        '</div>\n'
        f"{_progress_bar_html(model)}"
        f'<div class="dist">{dist}</div>\n'
        "</div>\n"
        f"{tables}"
        "</body>\n"
        "</html>\n"
    )
