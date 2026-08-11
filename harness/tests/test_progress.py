#!/usr/bin/env python
"""Tests for the progress side-channel (docs/design/timeout-liveness-watchdog.md §0)."""
from __future__ import annotations

import json

from harness.core.progress import (
    load_all_progress,
    progress_dir,
    progress_path,
    read_progress,
    write_progress,
)


def test_progress_dir_is_sibling_of_ledger(tmp_path) -> None:
    ledger_path = tmp_path / "ledger" / "events.jsonl"
    assert progress_dir(ledger_path) == (tmp_path / "ledger" / "progress")


def test_write_and_read_progress(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    write_progress("PA__hermes_0", ledger_path, vendor="hermes",
                    status="running", detail="step_update ACTIVE",
                    last_activity_ts=123.0)

    got = read_progress("PA__hermes_0", ledger_path)
    assert got == {
        "task_id": "PA__hermes_0",
        "vendor": "hermes",
        "status": "running",
        "detail": "step_update ACTIVE",
        "last_activity_ts": 123.0,
    }


def test_write_progress_is_overwrite_not_append(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    write_progress("PA", ledger_path, detail="first", last_activity_ts=1.0)
    write_progress("PA", ledger_path, detail="second", last_activity_ts=2.0)

    got = read_progress("PA", ledger_path)
    assert got["detail"] == "second"
    assert got["last_activity_ts"] == 2.0
    # exactly one line/object on disk, not an append log
    raw = progress_path("PA", ledger_path).read_text(encoding="utf-8")
    assert json.loads(raw)["detail"] == "second"


def test_write_progress_default_timestamp(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    before = __import__("time").time()
    write_progress("PA", ledger_path)
    after = __import__("time").time()
    got = read_progress("PA", ledger_path)
    assert before <= got["last_activity_ts"] <= after
    assert got["status"] == "running"


def test_read_progress_missing_returns_none(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    assert read_progress("nope", ledger_path) is None


def test_read_progress_unparseable_returns_none(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    p = progress_path("bad", ledger_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json", encoding="utf-8")
    assert read_progress("bad", ledger_path) is None


def test_load_all_progress(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    write_progress("PA", ledger_path, vendor="hermes", detail="a")
    write_progress("PB__agy_0", ledger_path, vendor="agy", detail="b")

    all_progress = load_all_progress(ledger_path)
    assert set(all_progress) == {"PA", "PB__agy_0"}
    assert all_progress["PA"]["vendor"] == "hermes"
    assert all_progress["PB__agy_0"]["vendor"] == "agy"


def test_load_all_progress_missing_dir_returns_empty(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    assert load_all_progress(ledger_path) == {}


def test_load_all_progress_skips_unparseable(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    write_progress("PA", ledger_path, detail="ok")
    progress_path("bad", ledger_path).write_text("{not json", encoding="utf-8")

    all_progress = load_all_progress(ledger_path)
    assert set(all_progress) == {"PA"}
