#!/usr/bin/env python
"""Concurrency + crash-recovery test for Ledger (H3 verification).

Chunk layout (spec.md "用語: 台帳の構造（1塊 = 1設計）"):
  one CHUNK = one line. A chunk bundles events for a (design_file, task_file) pair.

Run: python -m pytest harness/tests/test_ledger.py -q
"""
from __future__ import annotations

import os
import tempfile
import shutil
import threading
import subprocess
import sys

from harness.core.ledger import Ledger, Sequencer


def test_idempotent_dup() -> None:
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        lg = Ledger(p)
        k1 = lg.append_chunk("design.md", "tasks.md", [{"event_id": "T-1:1", "type": "task.created"}])
        # identical (design_file, task_file) chunk -> idempotent ignore
        k2 = lg.append_chunk("design.md", "tasks.md", [{"event_id": "T-1:2", "type": "task.created"}])
        assert k1 is not None
        assert k2 is None, "duplicate (design_file, task_file) chunk must be ignored"
        # different task_file -> distinct chunk
        k3 = lg.append_chunk("design.md", "tasks2.md", [{"event_id": "T-1:1", "type": "task.created"}])
        assert k3 is not None
        evs = lg.load()
        assert len(evs) == 2, len(evs)
    finally:
        shutil.rmtree(tmp)


def test_crash_partial_line() -> None:
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        lg = Ledger(p)
        lg.append_chunk("design.md", "tasks.md", [{"event_id": "T-1:1", "type": "task.created"}])
        lg.append_chunk("design.md", "tasks2.md", [{"event_id": "T-2:1", "type": "task.leased"}])
        with open(p, "a", encoding="utf-8") as fh:
            fh.write('{"design_file":"design.md","task_file":"tasks3.md","events":[{"event')  # partial, no newline
        lg2 = Ledger(p)
        chunks = lg2.load()
        assert len(chunks) == 2, len(chunks)  # partial dropped; load_flat also safe
        flat = lg2.load_flat()
        assert len(flat) == 2, len(flat)
    finally:
        shutil.rmtree(tmp)


def test_chunk_and_event_timestamps() -> None:
    """Chunks get created_at (set once) / updated_at (refreshed on every
    append); each event gets its own ts stamp."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        lg = Ledger(p)
        lg.append_event("design.md", "tasks.md", {"event_id": "T-1:1", "type": "task.created"})
        chunk = lg.load()[0]
        assert chunk["created_at"] > 0
        assert chunk["updated_at"] == chunk["created_at"]
        assert chunk["events"][0]["ts"] > 0

        created_at = chunk["created_at"]
        lg.append_event("design.md", "tasks.md", {"event_id": "T-1:2", "type": "task.implemented"})
        chunk = lg.load()[0]
        # created_at is stable across subsequent appends to the same chunk
        assert chunk["created_at"] == created_at
        assert chunk["updated_at"] >= created_at
        assert len(chunk["events"]) == 2
        assert chunk["events"][1]["ts"] >= chunk["events"][0]["ts"]
    finally:
        shutil.rmtree(tmp)


def test_sequencer_order() -> None:
    """N threads propose chunks through one Sequencer; final stream is ordered & unique."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        seq = Sequencer(p)
        seq.start()

        def worker(k: int) -> None:
            tf = f"tasks_{k}.md"
            seq.propose_chunk("design.md", tf, [
                {"event_id": f"T-{k}:1", "type": "task.created"},
                {"event_id": f"T-{k}:2", "type": "task.implemented"},
            ])

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seq.stop()

        chunks = Ledger(p).load()
        assert len(chunks) == 4, len(chunks)  # 4 distinct (design,task) chunks
        # every chunk's events are in append order
        for c in chunks:
            evs = c["events"]
            seqs = [int(e["event_id"].split(":")[1]) for e in evs]
            assert seqs == sorted(seqs), seqs
    finally:
        shutil.rmtree(tmp)


def test_resolve_design_file() -> None:
    """Sequencer.resolve_design_file() is the task -> design mirror of
    resolve_task_file() (design -> task)."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        seq = Sequencer(p)
        seq.start()
        seq.propose_chunk("design.md", "tasks.md", [{"event_id": "T-1:1", "type": "task.created"}])
        seq.stop()

        assert seq.resolve_design_file("tasks.md") == "design.md"
        # unrecorded task_file -> ""
        assert seq.resolve_design_file("nope.md") == ""

        # relative/absolute mismatch is resolved via path normalization
        abs_tasks = os.path.abspath("tasks.md")
        assert seq.resolve_design_file(abs_tasks) == "design.md"
    finally:
        shutil.rmtree(tmp)


def test_resolve_design_file_absolute_recorded() -> None:
    """Same lookup, but the ledger recorded task_file as an absolute path
    (as drive.py does) while the caller queries with a relative one."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        seq = Sequencer(p)
        seq.start()
        abs_tasks = os.path.abspath("tasks2.md")
        seq.propose_chunk("design2.md", abs_tasks, [{"event_id": "T-2:1", "type": "task.created"}])
        seq.stop()

        assert seq.resolve_design_file("tasks2.md") == "design2.md"
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_idempotent_dup()
    test_crash_partial_line()
    test_sequencer_order()
    test_resolve_design_file()
    test_resolve_design_file_absolute_recorded()
    print("ALL LEDGER TESTS PASSED")
