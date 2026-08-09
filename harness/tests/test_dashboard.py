#!/usr/bin/env python
"""Tests for dashboard role and build_model."""
from __future__ import annotations

from harness.roles.dashboard import (
    build_model,
    format_ts,
    group_by_design_file,
    progress_summary,
    render_html,
    render_markdown,
)


def _model(**tasks: str) -> dict:
    """Build a minimal model fixture: task_id -> status (meta fields empty)."""
    return {
        task_id: {
            "status": status, "design_file": "", "task_file": "",
            "created_at": "", "updated_at": "",
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
