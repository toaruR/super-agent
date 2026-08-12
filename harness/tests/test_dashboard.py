#!/usr/bin/env python
"""Tests for dashboard role and build_model."""
from __future__ import annotations

from harness.roles.dashboard import (
    build_model,
    format_ts,
    group_by_design_file,
    load_progress,
    progress_bar_segments,
    progress_summary,
    render_html,
    render_markdown,
)

# Fixed reference clock used by the stale tests (2021-01-01T00:00:00Z + 1 day).
NOW = 1609545600.0
HOUR = 3600.0


def _model(**tasks: str) -> dict:
    """Build a minimal model fixture: task_id -> status (meta fields empty)."""
    return {
        task_id: {
            "status": status, "design_file": "", "task_file": "",
            "created_at": "", "updated_at": "",
            "is_stale": False, "reason": "",
        }
        for task_id, status in tasks.items()
    }


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
    assert model["task-1"]["status"] == "integrated"
    assert model["task-2"]["status"] == "failed"
    assert model["task-3"]["status"] == "passed"
    assert model["task-4"]["status"] == "failed"
    assert model["task-5"]["status"] == "custom_status"


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
    assert model["task-1"]["status"] == "integrated"
    assert model["task-2"]["status"] == "failed"


def test_build_model_legacy_event_id_only() -> None:
    """Some vendor-produced events only carry `event_id` (no `task_id`).
    The id should be derived by stripping the `:<seq>` suffix."""
    events = [
        {"event_id": "legacy-9:7", "seq": 7, "type": "task.created"},
    ]
    model = build_model(events)
    assert set(model) == {"legacy-9"}
    assert model["legacy-9"]["status"] == "created"


def test_build_model_meta_design_file_and_timestamps() -> None:
    """design_file/task_file/created_at/updated_at are folded in from event
    fields (as populated by Ledger.load_flat() / Ledger.append_event())."""
    events = [
        {"task_id": "task-1", "type": "task.created",
         "design_file": "d.md", "task_file": "t.md", "ts": 100},
        {"task_id": "task-1", "type": "integrate.ok",
         "design_file": "d.md", "task_file": "t.md", "ts": 200},
    ]
    model = build_model(events)
    info = model["task-1"]
    assert info["design_file"] == "d.md"
    assert info["task_file"] == "t.md"
    assert info["created_at"] == 100
    assert info["updated_at"] == 200


def test_build_model_meta_missing_ts_defaults_empty() -> None:
    events = [{"task_id": "task-1", "type": "task.created"}]
    model = build_model(events)
    info = model["task-1"]
    assert info["design_file"] == ""
    assert info["created_at"] == ""
    assert info["updated_at"] == ""


def test_group_by_design_file() -> None:
    model = {
        "task-1": {"status": "integrated", "design_file": "b.md", "task_file": "",
                    "created_at": "", "updated_at": ""},
        "task-2": {"status": "failed", "design_file": "a.md", "task_file": "",
                    "created_at": "", "updated_at": ""},
        "task-3": {"status": "created", "design_file": "", "task_file": "",
                    "created_at": "", "updated_at": ""},
    }
    groups = group_by_design_file(model)
    # sorted alphabetically, with the unknown-design_file bucket last
    assert list(groups) == ["a.md", "b.md", "(design file unknown)"]
    assert set(groups["a.md"]) == {"task-2"}
    assert set(groups["b.md"]) == {"task-1"}
    assert set(groups["(design file unknown)"]) == {"task-3"}


def test_render_markdown_table() -> None:
    model = _model(**{"task-1": "integrated", "task-2": "failed"})
    md = render_markdown(model)
    assert md.startswith("# Dashboard")
    assert "| Task ID | Status | Created At | Updated At |" in md
    assert "| task-1 | integrated |" in md
    assert "| task-2 | failed |" in md


def test_render_markdown_groups_tasks_by_design_file() -> None:
    model = {
        "task-1": {
            "status": "integrated", "design_file": "design.md", "task_file": "tasks.md",
            "created_at": 0, "updated_at": 0,
        },
        "task-2": {
            "status": "failed", "design_file": "other.md", "task_file": "",
            "created_at": 0, "updated_at": 0,
        },
        "task-3": {
            "status": "created", "design_file": "", "task_file": "",
            "created_at": 0, "updated_at": 0,
        },
    }
    md = render_markdown(model)
    assert "### design.md" in md
    assert "### other.md" in md
    assert "### (design file unknown)" in md
    # design.md's heading precedes other.md's, which precedes the unknown bucket
    assert md.index("### design.md") < md.index("### other.md") < md.index("### (design file unknown)")


def test_render_html_table() -> None:
    model = _model(**{"task-1": "integrated", "task-2": "failed"})
    html = render_html(model)
    assert "<!DOCTYPE html>" in html
    assert "<th>Task ID</th><th>Status</th>" in html
    assert "<tr><td>task-1</td><td>" in html


def test_render_html_groups_tasks_by_design_file() -> None:
    model = {
        "task-1": {
            "status": "integrated", "design_file": "design.md", "task_file": "tasks.md",
            "created_at": 0, "updated_at": 0,
        },
        "task-2": {
            "status": "failed", "design_file": "", "task_file": "",
            "created_at": 0, "updated_at": 0,
        },
    }
    out = render_html(model)
    assert "<h3>design.md</h3>" in out
    assert "<h3>(design file unknown)</h3>" in out
    assert out.index("<h3>design.md</h3>") < out.index("<h3>(design file unknown)</h3>")


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
    assert model["T1"]["status"] == "integrated"


def test_build_model_priority_keeps_weak_judgment() -> None:
    """When no concrete progress state exists, a weak judgment event is
    preserved (it is weaker than even `created`)."""
    events = [
        {"task_id": "T2", "type": "judgment", "verdict": "unavailable"},
    ]
    model = build_model(events)
    assert model["T2"]["status"] == "judgment:unavailable"


def test_build_model_priority_equal_rank_latest_wins() -> None:
    """Same-rank states fall back to latest (chronological) event."""
    events = [
        {"task_id": "T3", "type": "task.implemented"},
        {"task_id": "T3", "type": "task.implemented"},
    ]
    model = build_model(events)
    assert model["T3"]["status"] == "implemented"


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
    assert model["PA"]["status"] == "passed"


def test_build_model_aggregates_meta_across_subchannels() -> None:
    """created_at/updated_at for a logical task span the earliest/latest
    timestamp across itself and all of its sub-channels."""
    events = [
        {"task_id": "PA", "type": "task.created", "ts": 300},
        {"task_id": "PA__hermes_0", "type": "task.implemented", "ts": 100},
        {"task_id": "PA__hermes_1", "type": "review.pass", "ts": 500},
    ]
    model = build_model(events)
    assert model["PA"]["created_at"] == 100
    assert model["PA"]["updated_at"] == 500


def test_build_model_transient_event_does_not_pin_status() -> None:
    """Requirement B: a transient event (verification.run) must not pin the
    task's status; a later terminal event overrides it."""
    events = [
        {"task_id": "T4", "type": "verification.run"},
        {"task_id": "T4", "type": "review.pass"},
    ]
    model = build_model(events)
    assert model["T4"]["status"] == "passed"


def test_render_markdown_summary() -> None:
    """Markdown output must include a progress-summary section with totals,
    completed count, rate and per-status distribution."""
    model = _model(**{"task-1": "integrated", "task-2": "failed", "task-3": "passed"})
    md = render_markdown(model)
    assert "## Progress Summary" in md
    assert "Total logical tasks: 3" in md
    assert "Completed (integrated/passed): 2 (66.7%)" in md
    assert "By status:" in md
    # the tasks table is still present
    assert "| task-1 | integrated |" in md


def test_render_html_dark_mode_and_badges() -> None:
    model = _model(**{"task-1": "integrated", "task-2": "failed", "task-3": "passed"})
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
    model = _model(a="integrated", b="passed", c="failed", d="scheduled")
    s = progress_summary(model)
    assert s["total"] == 4
    assert s["done"] == 2
    assert s["rate"] == 50.0
    assert s["counts"] == {
        "integrated": 1, "passed": 1, "failed": 1, "scheduled": 1
    }


def test_format_ts_empty() -> None:
    assert format_ts("") == "-"
    assert format_ts(None) == "-"
    assert format_ts(0) == "-"


def test_format_ts_formats_epoch() -> None:
    # 2021-01-01T00:00:00Z
    out = format_ts(1609459200)
    assert out != "-"
    assert "2020" in out or "2021" in out  # tz-dependent, just sanity check


# --- stale detection (build_model) -----------------------------------------


def _stale_events(status_type: str, ts: float) -> list[dict]:
    return [{"task_id": "T", "type": status_type, "ts": ts}]


def test_build_model_flags_stale_non_terminal_task() -> None:
    """A leased task untouched for longer than the threshold is stale."""
    model = build_model(_stale_events("task.leased", NOW - HOUR), now=NOW)
    assert model["T"]["status"] == "leased"
    assert model["T"]["is_stale"] is True


def test_build_model_fresh_non_terminal_task_is_not_stale() -> None:
    """Within the threshold (default 30 min) the task is healthy."""
    model = build_model(_stale_events("task.leased", NOW - 60), now=NOW)
    assert model["T"]["is_stale"] is False


def test_build_model_stale_threshold_is_thirty_minutes_by_default() -> None:
    """Boundary: exactly 30 minutes is not yet stale, 30 min + 1s is."""
    at_limit = build_model(_stale_events("task.leased", NOW - 1800), now=NOW)
    just_over = build_model(_stale_events("task.leased", NOW - 1801), now=NOW)
    assert at_limit["T"]["is_stale"] is False
    assert just_over["T"]["is_stale"] is True


def test_build_model_stale_after_is_overridable() -> None:
    """The threshold is injectable via the stale_after argument."""
    events = _stale_events("task.leased", NOW - 300)  # 5 minutes old
    assert build_model(events, now=NOW)["T"]["is_stale"] is False
    assert build_model(events, now=NOW, stale_after=60)["T"]["is_stale"] is True
    assert build_model(events, now=NOW, stale_after=HOUR)["T"]["is_stale"] is False


def test_build_model_all_non_terminal_statuses_can_be_stale() -> None:
    """created / scheduled / leased / implemented are all stale candidates."""
    for type_, status in (
        ("task.created", "created"),
        ("task.scheduled", "scheduled"),
        ("task.leased", "leased"),
        ("task.implemented", "implemented"),
    ):
        model = build_model(_stale_events(type_, NOW - HOUR), now=NOW)
        assert model["T"]["status"] == status
        assert model["T"]["is_stale"] is True, f"{status} should be stale"


def test_build_model_terminal_statuses_are_never_stale() -> None:
    """integrated / passed / failed are terminal: never flagged stale even
    when their last event is ancient."""
    for type_, status in (
        ("integrate.ok", "integrated"),
        ("review.pass", "passed"),
        ("review.fail", "failed"),
    ):
        model = build_model(_stale_events(type_, NOW - 30 * 24 * HOUR), now=NOW)
        assert model["T"]["status"] == status
        assert model["T"]["is_stale"] is False, f"{status} must not be stale"


def test_build_model_task_without_ts_is_not_stale() -> None:
    """No updated_at means we cannot prove the task is stuck."""
    model = build_model([{"task_id": "T", "type": "task.leased"}], now=NOW)
    assert model["T"]["updated_at"] == ""
    assert model["T"]["is_stale"] is False


def test_build_model_stale_uses_aggregated_updated_at() -> None:
    """A logical task is judged on the latest activity across its
    sub-channels, not on the parent's own (older) event."""
    events = [
        {"task_id": "PA", "type": "task.created", "ts": NOW - 10 * HOUR},
        {"task_id": "PA__hermes_0", "type": "task.leased", "ts": NOW - 60},
    ]
    model = build_model(events, now=NOW)
    assert model["PA"]["updated_at"] == NOW - 60
    assert model["PA"]["is_stale"] is False


def test_build_model_default_now_uses_wall_clock() -> None:
    """Omitting `now` falls back to the current time (no crash, recent event
    is not stale)."""
    import time

    model = build_model([{"task_id": "T", "type": "task.leased", "ts": time.time()}])
    assert model["T"]["is_stale"] is False


# --- failure reason (build_model) ------------------------------------------


def test_build_model_extracts_failure_reason_from_error() -> None:
    events = [
        {"task_id": "T", "type": "task.leased"},
        {"task_id": "T", "type": "implementer.error", "error": "vendor exited 1"},
    ]
    model = build_model(events, now=NOW)
    assert model["T"]["status"] == "failed"
    assert model["T"]["reason"] == "vendor exited 1"


def test_build_model_failure_reason_field_priority() -> None:
    """error → reason → why: the first present field wins."""
    all_three = build_model(
        [{"task_id": "T", "type": "review.fail",
          "error": "E", "reason": "R", "why": "W"}], now=NOW)
    assert all_three["T"]["reason"] == "E"

    reason_only = build_model(
        [{"task_id": "T", "type": "review.fail", "reason": "R", "why": "W"}],
        now=NOW)
    assert reason_only["T"]["reason"] == "R"

    why_only = build_model(
        [{"task_id": "T", "type": "review.fail", "why": "W"}], now=NOW)
    assert why_only["T"]["reason"] == "W"


def test_build_model_failure_reason_absent_stays_empty() -> None:
    """No reason field available → the model must not invent one."""
    model = build_model([{"task_id": "T", "type": "review.fail"}], now=NOW)
    assert model["T"]["status"] == "failed"
    assert model["T"]["reason"] == ""


def test_build_model_non_failed_task_has_no_reason() -> None:
    """An `error` payload on a non-failing task must not leak into reason."""
    events = [
        {"task_id": "T", "type": "implementer.error", "error": "transient boom"},
        {"task_id": "T", "type": "integrate.ok"},
    ]
    model = build_model(events, now=NOW)
    assert model["T"]["status"] == "integrated"
    assert model["T"]["reason"] == ""


def test_build_model_failure_reason_from_judgment_verdict() -> None:
    events = [
        {"task_id": "T", "type": "judgment", "verdict": "FAIL",
         "reason": "acceptance criteria not met"},
    ]
    model = build_model(events, now=NOW)
    assert model["T"]["status"] == "failed"
    assert model["T"]["reason"] == "acceptance criteria not met"


def test_build_model_failure_reason_survives_bare_failure_event() -> None:
    """A bare `review.fail` followed by a detailed error event keeps the
    detailed reason (same rank → first available reason wins)."""
    events = [
        {"task_id": "T", "type": "review.fail"},
        {"task_id": "T", "type": "implementer.error", "error": "detailed boom"},
    ]
    model = build_model(events, now=NOW)
    assert model["T"]["status"] == "failed"
    assert model["T"]["reason"] == "detailed boom"


def test_build_model_failure_reason_aggregated_from_subchannel() -> None:
    events = [
        {"task_id": "PA", "type": "task.created"},
        {"task_id": "PA__hermes_0", "type": "implementer.error", "error": "sub boom"},
    ]
    model = build_model(events, now=NOW)
    assert model["PA"]["status"] == "failed"
    assert model["PA"]["reason"] == "sub boom"


# --- progress summary / bar segments ---------------------------------------


def test_progress_summary_counts_stale() -> None:
    model = _model(a="leased", b="integrated")
    model["a"]["is_stale"] = True
    s = progress_summary(model)
    assert s["stale"] == 1


def test_progress_bar_segments_empty_model() -> None:
    assert progress_bar_segments({}) == []


def test_progress_bar_segments_percentages_sum_to_100() -> None:
    model = _model(a="integrated", b="passed", c="implemented",
                   d="failed", e="scheduled")
    segments = progress_bar_segments(model)
    labels = {label: (cls, count) for label, cls, count, _ in segments}
    assert labels["Completed"] == ("bar-green", 2)
    assert labels["In Progress"] == ("bar-blue", 1)
    assert labels["Failed"] == ("bar-red", 1)
    assert labels["Pending"] == ("bar-gray", 1)
    assert abs(sum(pct for *_x, pct in segments) - 100.0) < 1e-6


def test_progress_bar_segments_stale_has_its_own_bucket() -> None:
    """A stale task is counted in the orange stale segment, not in the blue
    in-progress one."""
    model = _model(a="leased", b="leased")
    model["a"]["is_stale"] = True
    segments = progress_bar_segments(model)
    labels = {label: (cls, count) for label, cls, count, _ in segments}
    assert labels["Stale"] == ("bar-orange", 1)
    assert labels["In Progress"] == ("bar-blue", 1)


def test_progress_bar_segments_unbucketed_status_goes_to_other() -> None:
    model = _model(a="integrated", b="custom_status")
    segments = progress_bar_segments(model)
    labels = {label: count for label, _cls, count, _pct in segments}
    assert labels["Other"] == 1
    assert abs(sum(pct for *_x, pct in segments) - 100.0) < 1e-6


# --- HTML rendering: stale / reason / progress bar --------------------------


def test_render_html_stale_badge_is_orange_and_labelled() -> None:
    model = _model(fresh="leased", stuck="leased")
    model["stuck"]["is_stale"] = True
    out = render_html(model)
    assert 'class="badge badge-orange"' in out
    assert "Stale" in out
    # the healthy task keeps the blue in-progress badge
    assert 'class="badge badge-blue"' in out


def test_render_html_no_stale_badge_when_nothing_is_stale() -> None:
    out = render_html(_model(a="leased"))
    assert "badge-orange" not in out.split("<style>")[-1].split("</style>")[-1]


def test_render_html_shows_failure_reason_under_badge() -> None:
    model = _model(a="failed")
    model["a"]["reason"] = "vendor exited 1"
    out = render_html(model)
    assert 'class="reason"' in out
    assert "vendor exited 1" in out


def test_render_html_long_reason_is_truncated_with_title() -> None:
    long_reason = "x" * 200
    model = _model(a="failed")
    model["a"]["reason"] = long_reason
    out = render_html(model)
    # full text preserved in the title attribute
    assert f'title="{long_reason}"' in out
    # but the inline text is shortened
    assert f">{long_reason}<" not in out
    assert "…" in out


def test_render_html_no_reason_element_when_reason_missing() -> None:
    out = render_html(_model(a="failed"))
    assert 'class="reason"' not in out


def test_render_html_progress_bar_present_with_colour_segments() -> None:
    model = _model(a="integrated", b="implemented", c="failed", d="scheduled")
    model["e"] = dict(model["a"], status="leased", is_stale=True)
    out = render_html(model)
    assert 'class="progress-bar"' in out
    assert "bar-green" in out
    assert "bar-blue" in out
    assert "bar-red" in out
    assert "bar-gray" in out
    assert "bar-orange" in out
    # existing numeric metrics + distribution badges are preserved
    assert "Logical Tasks" in out
    assert "Completed" in out
    assert 'class="dist"' in out


def test_render_html_progress_bar_absent_for_empty_model() -> None:
    out = render_html({})
    assert 'class="progress-bar"' not in out


def test_render_html_summary_dist_includes_stale_badge() -> None:
    model = _model(a="leased")
    model["a"]["is_stale"] = True
    out = render_html(model)
    assert "stale=1" in out


# --- Markdown rendering: stale / reason -------------------------------------


def test_render_markdown_marks_stale_rows() -> None:
    model = _model(fresh="leased", stuck="leased")
    model["stuck"]["is_stale"] = True
    md = render_markdown(model)
    assert "| stuck | leased ⚠ stale |" in md
    assert "| fresh | leased |" in md
    assert "Stale (no progress for a while): 1" in md


def test_render_markdown_has_reason_column() -> None:
    model = _model(a="failed", b="integrated")
    model["a"]["reason"] = "vendor exited 1"
    md = render_markdown(model)
    assert "| Task ID | Status | Created At | Updated At | Reason |" in md
    assert "vendor exited 1" in md
    # no reason → the column stays blank, nothing invented
    assert "| b | integrated | - | - |  |" in md


def test_render_markdown_reason_pipes_are_escaped() -> None:
    model = _model(a="failed")
    model["a"]["reason"] = "cmd | grep failed"
    md = render_markdown(model)
    assert "cmd \\| grep failed" in md


def test_render_markdown_table_prefix_stays_cli_compatible() -> None:
    """Legacy consumers grep for `| <id> | <status> |`; adding the reason
    column must not break that prefix."""
    md = render_markdown(_model(**{"task-1": "integrated"}))
    assert "| task-1 | integrated |" in md


# --- progress side-channel ingestion (build_model) ---------------------------


def test_build_model_uses_progress_last_activity_for_stale_detection() -> None:
    """A non-terminal task whose ledger updated_at is old but whose progress
    side-channel keeps heartbeating is NOT stale (docs/design/
    timeout-liveness-watchdog.md §4)."""
    events = [{"task_id": "T", "type": "task.leased", "ts": NOW - 10 * HOUR}]
    progress = {"T": {"last_activity_ts": NOW - 30, "detail": "still working"}}
    model = build_model(events, now=NOW, progress=progress)
    assert model["T"]["is_stale"] is False
    assert model["T"]["last_activity_ts"] == NOW - 30


def test_build_model_progress_older_than_ledger_does_not_help() -> None:
    """If the progress heartbeat is itself old, max(updated_at, last_activity_ts)
    still reflects the ledger's fresher timestamp -- and vice versa: staleness
    is judged on whichever signal is more recent."""
    events = [{"task_id": "T", "type": "task.leased", "ts": NOW - 30}]
    progress = {"T": {"last_activity_ts": NOW - 10 * HOUR, "detail": "stuck"}}
    model = build_model(events, now=NOW, progress=progress)
    assert model["T"]["is_stale"] is False


def test_build_model_progress_missing_task_id_is_ignored() -> None:
    """A progress dict that doesn't mention this task_id changes nothing."""
    events = [{"task_id": "T", "type": "task.leased", "ts": NOW - HOUR}]
    model = build_model(events, now=NOW, progress={"OTHER": {"last_activity_ts": NOW}})
    assert model["T"]["is_stale"] is True
    assert model["T"]["last_activity_ts"] == ""


def test_build_model_progress_aggregates_across_subchannels() -> None:
    """A sub-channel's progress heartbeat keeps the whole logical task fresh,
    same as ledger updated_at aggregation."""
    events = [
        {"task_id": "PA", "type": "task.created", "ts": NOW - 10 * HOUR},
        {"task_id": "PA__hermes_0", "type": "task.leased", "ts": NOW - 10 * HOUR},
    ]
    progress = {"PA__hermes_0": {"last_activity_ts": NOW - 5, "detail": "running"}}
    model = build_model(events, now=NOW, progress=progress)
    assert model["PA"]["is_stale"] is False
    assert model["PA"]["last_activity_ts"] == NOW - 5


def test_build_model_last_activity_display_precomputed() -> None:
    events = [{"task_id": "T", "type": "task.leased", "ts": NOW - 90}]
    model = build_model(events, now=NOW)
    assert model["T"]["last_activity_display"] == "1m ago"


def test_build_model_last_activity_display_dash_when_no_ts() -> None:
    model = build_model([{"task_id": "T", "type": "task.leased"}], now=NOW)
    assert model["T"]["last_activity_display"] == "-"


def test_render_markdown_has_last_activity_column() -> None:
    model = _model(**{"task-1": "integrated"})
    model["task-1"]["last_activity_display"] = "5m ago"
    md = render_markdown(model)
    assert "| Task ID | Status | Created At | Updated At | Reason | Last Activity |" in md
    assert "5m ago" in md


def test_render_html_has_last_activity_column() -> None:
    model = _model(**{"task-1": "integrated"})
    model["task-1"]["last_activity_display"] = "5m ago"
    out = render_html(model)
    assert "<th>Last Activity</th>" in out
    assert "5m ago" in out


def test_render_html_refresh_meta_absent_by_default() -> None:
    out = render_html(_model(**{"task-1": "integrated"}))
    assert "http-equiv=\"refresh\"" not in out


def test_render_html_refresh_meta_present_when_requested() -> None:
    out = render_html(_model(**{"task-1": "integrated"}), refresh_interval=5)
    assert '<meta http-equiv="refresh" content="5">' in out


def test_load_progress_delegates_to_progress_module(tmp_path) -> None:
    from harness.core.progress import write_progress

    ledger_path = tmp_path / "events.jsonl"
    write_progress("T1", ledger_path, vendor="hermes", status="running", detail="working")
    progress = load_progress(str(ledger_path))
    assert progress["T1"]["vendor"] == "hermes"
    assert progress["T1"]["status"] == "running"


def test_build_model_retry_failed_task_updates_status_to_running() -> None:
    """When a previously failed task is retried (implementer.invoked emitted at a later ts),
    the dashboard model must transition its status to 'running' and clear the reason."""
    events = [
        {"task_id": "T1", "type": "task.created", "ts": 100},
        {"task_id": "T1", "type": "implementer.error", "error": "timeout boom", "ts": 200},
        {"task_id": "T1", "type": "implementer.invoked", "vendor": "hermes", "ts": 300},
    ]
    model = build_model(events, now=310)
    assert model["T1"]["status"] == "running"
    assert model["T1"]["reason"] == ""


def test_build_model_progress_running_overrides_failed_status() -> None:
    """When a task has a past failure in ledger, but has an active progress heartbeat with
    status='running' and newer ts, the dashboard model shows status='running'."""
    events = [
        {"task_id": "T1", "type": "implementer.error", "error": "timeout boom", "ts": 200},
    ]
    progress = {
        "T1": {"task_id": "T1", "status": "running", "detail": "retrying...", "last_activity_ts": 300}
    }
    model = build_model(events, now=310, progress=progress)
    assert model["T1"]["status"] == "running"
    assert model["T1"]["reason"] == ""


def test_build_model_reviewing_status() -> None:
    """When a reviewer is invoked, the task status becomes 'reviewing'."""
    events = [
        {"task_id": "T1", "type": "task.created", "ts": 100},
        {"task_id": "T1", "type": "task.implemented", "ts": 200},
        {"task_id": "T1", "type": "reviewer.invoked", "vendor": "codex", "ts": 300},
    ]
    model = build_model(events, now=310)
    assert model["T1"]["status"] == "reviewing"
    assert model["T1"]["reason"] == ""


def test_build_model_judgment_unavailable_reason() -> None:
    """When a review produces judgment_unavailable or fails, why is stored in reason."""
    events = [
        {"task_id": "T1", "type": "reviewer.invoked", "vendor": "codex", "ts": 100},
        {"task_id": "T1", "type": "judgment", "verdict": "judgment_unavailable", "why": "reviewer produced no parseable output", "ts": 200},
    ]
    model = build_model(events, now=210)
    assert model["T1"]["status"] == "failed"
    assert model["T1"]["reason"] == "reviewer produced no parseable output"


