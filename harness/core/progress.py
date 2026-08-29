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

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from harness.core.ledger import normalize_path


def progress_dir(ledger_path: str | Path) -> Path:
    """The progress directory for a given ledger path (its sibling `progress/`)."""
    return Path(ledger_path).resolve().parent / "progress"


def _design_tag(design_file: str) -> str:
    """Short filesystem-safe tag for a design_file, appended to the progress
    filename so two designs that mint the same task_id (the decomposer always
    numbers tasks "T1", "T2", ... independently per design -- see
    harness/roles/dashboard.py's build_model()) never share one heartbeat
    file on disk. Hashed via the same normalize_path() harness.core.ledger
    uses to stamp design_file on ledger events, so a caller passing either a
    relative or absolute path for the same file resolves to the same tag.
    Empty when no design_file is given, so untagged callers keep the legacy
    bare "<task_id>.json" filename."""
    if not design_file:
        return ""
    return hashlib.sha1(normalize_path(design_file).encode("utf-8")).hexdigest()[:8]


def progress_path(task_id: str, ledger_path: str | Path, design_file: str = "") -> Path:
    tag = _design_tag(design_file)
    name = f"{task_id}__{tag}.json" if tag else f"{task_id}.json"
    return progress_dir(ledger_path) / name


def write_progress(
    task_id: str,
    ledger_path: str | Path,
    *,
    design_file: str = "",
    vendor: str = "",
    status: str = "running",
    detail: str = "",
    last_activity_ts: float | None = None,
) -> Path:
    """Overwrite the progress file for one (design_file, task_id) pair
    (liveness heartbeat).

    Atomic write via temp-file + os.replace so a concurrent reader (dashboard)
    never observes a half-written file. Pass the same design_file the caller
    uses for its ledger events so heartbeats stay isolated when two designs
    happen to share a task_id (see _design_tag()).
    """
    d = progress_dir(ledger_path)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "design_file": design_file,
        "vendor": vendor,
        "status": status,
        "detail": detail,
        "last_activity_ts": (
            last_activity_ts if last_activity_ts is not None else time.time()
        ),
    }
    path = progress_path(task_id, ledger_path, design_file)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_progress(
    task_id: str, ledger_path: str | Path, design_file: str = ""
) -> dict[str, Any] | None:
    """Read one task's progress file. Returns None if missing or unparseable.

    design_file must match what write_progress() was called with for this
    task, otherwise the (design-scoped) file simply won't be found.
    """
    path = progress_path(task_id, ledger_path, design_file)
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

    Each record now carries its own "design_file" (may be "" for legacy/
    untagged writers). If more than one design_file's progress file maps to
    the same task_id -- two designs racing on a colliding id such as "T1" --
    the record with the most recent last_activity_ts wins, so the result
    stays a deterministic single record per task_id (glob() order is not) and
    a bare model["T1"] lookup keeps working instead of one write randomly
    clobbering the other's slot depending on filesystem enumeration order.
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
        existing = out.get(task_id)
        if existing is not None:
            prev_ts = float(existing.get("last_activity_ts") or 0)
            new_ts = float(payload.get("last_activity_ts") or 0)
            if new_ts <= prev_ts:
                continue
        out[task_id] = payload
    return out
