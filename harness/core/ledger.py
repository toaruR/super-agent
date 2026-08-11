#!/usr/bin/env python
"""Append-only ledger with the atomicity rules from ARCHITECTURE.md §5.1 (H3).

Chunk-based layout (spec.md "用語: 台帳の構造（1塊 = 1設計）"):
  - one CHUNK  = one line = one append write (newline terminated)
  - a chunk bundles all events for a single (design_file, task_file) pair
  - chunk schema: {"design_file": ..., "task_file": ..., "created_at": ...,
    "updated_at": ..., "events": [ ... ]}
  - chunk-level created_at is set once, when the chunk is first written;
    updated_at is refreshed on every subsequent event append
  - each individual event is stamped with "ts" (unix epoch seconds) the first
    time it is appended, so downstream consumers (harness.roles.dashboard) can
    derive a per-task created_at/updated_at even when several logical tasks
    share one (design_file, task_file) chunk

Design rules:
  - a repeat of the same (design_file, task_file) chunk in one session is ignored
    (idempotent) — the Sequencer tracks seen (design_file, task_file) keys
  - on load, a trailing line not ending in newline is discarded (crash recovery)
  - only the Sequencer process appends; others hand proposals to it
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _normalize_path(path: str) -> str:
    """Normalize a non-empty path string to a resolved absolute path string.
    Returns "" if path is empty."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return path


def _same_path(a: str, b: str) -> bool:
    """True if `a` and `b` refer to the same filesystem path.

    design_file/task_file strings in the ledger are recorded in normalized
    absolute form, but string comparison handles cases where one or both
    are normalized.
    """
    if not a or not b:
        return False
    return _normalize_path(a) == _normalize_path(b)


@dataclass
class Ledger:
    path: str
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _seen: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        # recover seen (design_file, task_file) keys from existing well-formed lines
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.endswith("\n"):
                        continue  # partial trailing line: discard on crash recovery
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._register(chunk)

    def _register(self, chunk: dict[str, Any]) -> None:
        df = chunk.get("design_file", "")
        tf = chunk.get("task_file", "")
        self._seen.add((df, tf))

    def append_chunk(self, design_file: str, task_file: str = "",
                     events: list[dict[str, Any]] | None = None) -> str | None:
        """Append one chunk (one (design_file, task_file) bundle). Returns the
        chunk key, or None if an identical (design_file, task_file) was already
        recorded this session (idempotent ignore).

        task_file may be empty/no key when the task DAG is not yet settled (e.g.
        during the require->decompose phase). An empty task_file signals 'no tasks
        defined yet' and is a valid, distinct chunk.
        """
        design_file = _normalize_path(design_file)
        task_file = _normalize_path(task_file)
        if events is None:
            events = []
        key = (design_file, task_file)
        with self._lock:
            if key in self._seen:
                return None  # duplicate chunk -> idempotent ignore
            now = time.time()
            stamped = []
            for ev in events:
                e = dict(ev)
                e.setdefault("ts", now)
                stamped.append(e)
            chunk: dict[str, Any] = {"design_file": design_file, "task_file": task_file}
            chunk["created_at"] = now
            chunk["updated_at"] = now
            chunk["events"] = stamped
            line = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)  # single append; OS guarantees atomicity at line size
            self._register(chunk)
            return f"{design_file}|{task_file}"

    def append_event(self, design_file: str, task_file: str,
                     event: dict[str, Any]) -> str | None:
        """Merge one event into the design_file's chunk.

        If a chunk for that design_file already exists, its events list is extended
        (and task_file is updated if currently empty and a non-empty task_file is given),
        and the whole file is rewritten atomically (temp + rename). Otherwise a new
        chunk line is appended.
        """
        design_file = _normalize_path(design_file)
        task_file = _normalize_path(task_file)
        with self._lock:
            chunks = self.load()
            target = None
            for c in chunks:
                if _same_path(c.get("design_file", ""), design_file):
                    target = c
                    break
            now = time.time()
            ev = dict(event)
            ev.setdefault("ts", now)
            if target is None:
                target = {"design_file": design_file, "task_file": task_file}
                target["created_at"] = now
                target["events"] = []
                chunks.append(target)
            else:
                if task_file and not target.get("task_file"):
                    target["task_file"] = task_file
            target["updated_at"] = now
            target.setdefault("events", []).append(ev)
            self._rewrite(chunks)
            self._register(target)
            return f"{design_file}|{task_file}"

    def _rewrite(self, chunks: list[dict[str, Any]]) -> None:
        """Rewrite the whole ledger file atomically (temp + rename)."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for c in chunks:
                fh.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, self.path)

    def load(self) -> list[dict[str, Any]]:
        """Reconstruct the full chunk stream (crash-safe: drops partial tail)."""
        if not os.path.exists(self.path):
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for raw in fh:
                if not raw.endswith("\n"):
                    continue  # partial trailing line from a crash -> discard
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out

    def load_flat(self) -> list[dict[str, Any]]:
        """Flatten all chunks into a single list of events, each annotated with
        its source design_file / task_file for traceability."""
        flat: list[dict[str, Any]] = []
        for chunk in self.load():
            df = chunk.get("design_file", "")
            tf = chunk.get("task_file", "")
            for ev in chunk.get("events", []):
                e = dict(ev)
                e.setdefault("design_file", df)
                e.setdefault("task_file", tf)
                flat.append(e)
        return flat


class Sequencer:
    """Only process that appends to events.jsonl. Others hand proposals here.

    Centralizes writes so concurrent processes can never interleave or race on the
    file (ARCHITECTURE.md §5.1, H3).
    """

    def __init__(self, path: str) -> None:
        self._ledger = Ledger(path)
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # design_file -> task_file, populated synchronously in propose_chunk()
        # so resolve_task_file() doesn't have to wait for the background
        # writer thread to flush a just-proposed chunk to disk (race fix).
        self._task_file_cache: dict[str, str] = {}

    @property
    def path(self) -> str:
        """The ledger file path (so callers can derive sibling dirs, e.g. the
        progress side-channel, without reaching into ``_ledger``)."""
        return self._ledger.path

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            # item holds {design_file, task_file, events:[event]}
            design_file = item["design_file"]
            task_file = item["task_file"]
            for ev in item["events"]:
                self._ledger.append_event(design_file, task_file, ev)

    def propose_chunk(self, design_file: str, task_file: str,
                      events: list[dict[str, Any]]) -> None:
        design_file = _normalize_path(design_file)
        task_file = _normalize_path(task_file)
        if task_file:
            self._task_file_cache[design_file] = task_file
        self._queue.put({
            "design_file": design_file,
            "task_file": task_file,
            "events": events,
        })

    def propose(self, task_id: str, type_: str, **fields: Any) -> None:
        """Queue one event under (design_file, task_file).

        If task_file is omitted, resolve it from the ledger via the
        design_file (callers in the drive phase always pass task_file
        explicitly; downstream roles recover it from the ledger).
        """
        design_file = _normalize_path(fields.pop("design_file", ""))
        task_file = _normalize_path(fields.pop("task_file", ""))
        if not task_file:
            task_file = self.resolve_task_file(design_file)
        self.propose_chunk(design_file, task_file, [{
            "event_id": f"{task_id}:0",
            "type": type_,
            **fields,
        }])

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def load(self) -> list[dict[str, Any]]:
        """Read the full chunk stream (delegates to the underlying Ledger)."""
        return self._ledger.load()

    def resolve_task_file(self, design_file: str) -> str:
        """Return the task_file registered for a design_file, or '' if none.

        Used by propose() so downstream roles can recover task_file from the
        ledger instead of receiving it as a function argument.

        Checks the in-memory cache first: propose_chunk() writes to it
        synchronously, but the actual disk write happens later on the
        background writer thread, so a disk-only lookup here would race
        against very recently queued (not-yet-flushed) proposals.
        """
        design_file = _normalize_path(design_file)
        cached = self._task_file_cache.get(design_file)
        if cached:
            return cached
        for chunk in self._ledger.load():
            if _same_path(chunk.get("design_file", ""), design_file) and chunk.get("task_file"):
                return chunk["task_file"]
        return ""

    def resolve_design_file(self, task_file: str) -> str:
        """Return the design_file registered for a task_file, or '' if none.

        Mirror of resolve_task_file() (task -> design instead of design ->
        task). Used by cli.resolve_design_file_arg() to recover --design_file
        when only --task_file is given and the file is already registered
        in the ledger.
        Reads the ledger directly (no cache): callers use this once, at the
        CLI entry point, before any writes for this invocation happen, so
        there is no race against the background writer thread to guard
        against (unlike resolve_task_file(), which propose() calls mid-flight).
        """
        task_file = _normalize_path(task_file)
        for chunk in self._ledger.load():
            df = chunk.get("design_file", "")
            tf = chunk.get("task_file", "")
            if df and _same_path(tf, task_file):
                return df
        return ""

    def load_flat(self) -> list[dict[str, Any]]:
        """Flatten all chunks into a single event list."""
        return self._ledger.load_flat()

