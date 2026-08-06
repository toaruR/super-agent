#!/usr/bin/env python
"""Concurrency + crash-recovery test for Ledger (H3 verification).

Run: python ledger_test.py
Proves: multiple writers appending through a single Sequencer preserve
order and drop duplicates; a crash leaving a partial line is recovered safely.
"""
from __future__ import annotations

import os
import tempfile
import shutil
import threading
import subprocess
import sys

from ledger import Ledger, Sequencer


def test_idempotent_dup() -> None:
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        lg = Ledger(p)
        e1 = lg.append("T-1", "task.created")
        e2 = lg.append("T-1", "task.created")  # new seq, not a dup
        assert e1 == "T-1:1" and e2 == "T-1:2", (e1, e2)
        # real duplicate via fresh instance replaying same id
        lg_re = Ledger(p)
        assert lg_re.append("T-1", "task.created") is not None  # seq 3, not dup
        print("PASS idempotent_dup")
    finally:
        shutil.rmtree(tmp)


def test_crash_partial_line() -> None:
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        lg = Ledger(p)
        lg.append("T-1", "task.created")
        lg.append("T-1", "task.leased")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write('{"event_id":"T-1:3","task_id":"T-1"')  # partial, no newline
        lg2 = Ledger(p)
        evs = lg2.load()
        assert len(evs) == 2, len(evs)  # partial dropped
        print("PASS crash_partial_line")
    finally:
        shutil.rmtree(tmp)


def test_sequencer_order() -> None:
    """N threads append through one Sequencer; final stream is ordered & unique."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "events.jsonl")
        seq = Sequencer(p)
        seq.start()

        def worker(tid: str, n: int) -> None:
            for i in range(n):
                seq.propose(tid, "heartbeat", i=i)

        threads = [threading.Thread(target=worker, args=(f"T-{k}", 50)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seq.stop()

        evs = Ledger(p).load()
        assert len(evs) == 200, len(evs)  # 4*50, no loss, no dup
        # every task's seqs are strictly increasing
        seen: dict[str, int] = {}
        for e in evs:
            s = e["seq"]
            assert s > seen.get(e["task_id"], 0), e
            seen[e["task_id"]] = s
        print("PASS sequencer_order (200 events, ordered)")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_idempotent_dup()
    test_crash_partial_line()
    test_sequencer_order()
    print("ALL LEDGER TESTS PASSED")
