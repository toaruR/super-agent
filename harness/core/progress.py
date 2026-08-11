#!/usr/bin/env python
"""Progress side-channel (liveness heartbeat).

See docs/design/timeout-liveness-watchdog.md §0. One overwrite-style JSON file
per task_id (sub-channel name included, e.g. ``PA__hermes_0``) under
``<ledger_dir>/progress/<task_id>.json``.

Deliberately NOT routed through Ledger/Sequencer: ``Ledger.append_event()``
rewrites the whole ledger file on every call (harness/core/ledger.py), so
streaming heartbeats through it would be O(n^2) write amplification over a
long-running task. The ledger keeps recording only start/end milestone
events; this side channel is for high-frequency liveness updates only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def progress_dir(ledger_path: str | Path) -> Path:
    """The progress directory for a given ledger path (its sibling `progress/`)."""
    return Path(ledger_path).resolve().parent / "progress"


def progress_path(task_id: str, ledger_path: str | Path) -> Path:
    return progress_dir(ledger_path) / f"{task_id}.json"


def write_progress(
    task_id: str,
    ledger_path: str | Path,
    *,
    vendor: str = "",
    status: str = "running",
    detail: str = "",
    last_activity_ts: float | None = None,
) -> Path:
    """Overwrite the progress file for one task_id (liveness heartbeat).

    Atomic write via temp-file + os.replace so a concurrent reader (dashboard)
    never observes a half-written file.
    """
    d = progress_dir(ledger_path)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "vendor": vendor,
        "status": status,
        "detail": detail,
        "last_activity_ts": (
            last_activity_ts if last_activity_ts is not None else time.time()
        ),
    }
    path = progress_path(task_id, ledger_path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_progress(task_id: str, ledger_path: str | Path) -> dict[str, Any] | None:
    """Read one task's progress file. Returns None if missing or unparseable."""
    path = progress_path(task_id, ledger_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_all_progress(ledger_path: str | Path) -> dict[str, dict[str, Any]]:
    """Load every progress file under the progress dir, keyed by task_id.

    Unparseable files are skipped (best-effort; a torn read must never crash
    the dashboard). Used by dashboard.py's stale-detection (phase 4).
    """
    d = progress_dir(ledger_path)
    if not d.is_dir():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for p in d.glob("*.json"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        task_id = payload.get("task_id") or p.stem
        out[task_id] = payload
    return out
