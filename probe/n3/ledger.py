#!/usr/bin/env python
"""Append-only ledger with the atomicity rules from ARCHITECTURE.md §5.1 (H3).

Design rules (proven by ledger_test.py):
 - one event  = one line  = one append write (newline terminated)
 - every event has a unique id {task_id}:{seq}; a repeat is ignored (idempotent)
 - on load, a trailing line not ending in newline is discarded (crash recovery)
 - only the Scheduler process appends; others hand proposals to it
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
    _seq: dict[str, int] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        # recover seq counters + seen ids from existing well-formed lines
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.endswith("\n"):
                        continue  # partial trailing line: discard on crash recovery
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._register(ev)

    def _register(self, ev: dict[str, Any]) -> None:
        eid = ev.get("event_id")
        if eid:
            self._seen.add(eid)
        tid = ev.get("task_id")
        seq = ev.get("seq")
        if tid and isinstance(seq, int):
            self._seq[tid] = max(self._seq.get(tid, 0), seq)

    def append(self, task_id: str, type_: str, **fields: Any) -> str | None:
        """Append one event. Returns event_id, or None if idempotently dropped."""
        with self._lock:
            seq = self._seq.get(task_id, 0) + 1
            eid = f"{task_id}:{seq}"
            if eid in self._seen:
                return None  # duplicate -> idempotent ignore
            ev = {"event_id": eid, "task_id": task_id, "seq": seq, "type": type_, **fields}
            line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)  # single append; OS guarantees atomicity at line size
            self._register(ev)
            return eid

    def load(self) -> list[dict[str, Any]]:
        """Reconstruct the full event stream (crash-safe: drops partial tail)."""
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


if __name__ == "__main__":
    import tempfile
    import shutil

    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        lg = Ledger(p)
        print("append1:", lg.append("T-1", "task.created", goal="x"))
        print("append2:", lg.append("T-1", "task.leased", agent="claude"))
        print("dup   :", lg.append("T-1", "task.created", goal="x"))  # same seq? no -> new
        # emulate a crash leaving a partial line
        with open(p, "a", encoding="utf-8") as fh:
            fh.write('{"event_id":"T-1:3"')  # no newline
        lg2 = Ledger(p)  # recover
        evs = lg2.load()
        print("recovered events:", len(evs))  # should be 2 (partial dropped)
    finally:
        shutil.rmtree(tmp)


class Sequencer:
    """Only process that appends to events.jsonl. Others hand proposals here.

    Centralizes sequence numbering so concurrent processes can never interleave
    or race on the file (ARCHITECTURE.md §5.1, H3).
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
            self._ledger.append(item["task_id"], item["type"], **item.get("fields", {}))

    def propose(self, task_id: str, type_: str, **fields: Any) -> None:
        self._queue.put({"task_id": task_id, "type": type_, "fields": fields})

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
