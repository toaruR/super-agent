#!/usr/bin/env python
"""Append-only ledger with the atomicity rules from ARCHITECTURE.md §5.1 (H3).

Chunk-based layout (spec.md "用語: 台帳の構造（1塊 = 1設計）"):
  - one CHUNK  = one line = one append write (newline terminated)
  - a chunk bundles all events for a single (design_file, task_file) pair
  - chunk schema: {"design_file": ..., "task_file": ..., "events": [ ... ]}

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
from dataclasses import dataclass, field
from typing import Any


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
        if events is None:
            events = []
        key = (design_file, task_file)
        with self._lock:
            if key in self._seen:
                return None  # duplicate chunk -> idempotent ignore
            chunk: dict[str, Any] = {"design_file": design_file}
            if task_file:
                chunk["task_file"] = task_file
            chunk["events"] = events
            line = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)  # single append; OS guarantees atomicity at line size
            self._register(chunk)
            return f"{design_file}|{task_file}"

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

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._ledger.append_chunk(
                item["design_file"], item["task_file"], item["events"])

    def propose_chunk(self, design_file: str, task_file: str,
                      events: list[dict[str, Any]]) -> None:
        self._queue.put({
            "design_file": design_file,
            "task_file": task_file,
            "events": events,
        })

    def propose(self, task_id: str, type_: str, **fields: Any) -> None:
        """Legacy single-event wrapper: emits a chunk with no task_file (task not
        yet settled) containing one event. New code should use propose_chunk."""
        self.propose_chunk("", "", [{
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

    def load_flat(self) -> list[dict[str, Any]]:
        """Flatten all chunks into a single event list."""
        return self._ledger.load_flat()
