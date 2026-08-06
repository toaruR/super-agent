#!/usr/bin/env python
"""Minimal scheduler + CLI (Stage A skeleton).

Records task lifecycle events to the ledger (H3-safe via Sequencer) and drives
vendor invocations through the adapter. Full decomposition/parallel/lease logic
lands in Stage B; this skeleton proves the ledger + invoke path end-to-end.

Usage:
  python -m harness.cli run "build a fizzbuzz module" [--dry-run]
  python -m harness.cli status
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from harness.core.invoke import invoke, load_vendors
from harness.core.ledger import Ledger, Sequencer

CONFIG_DIR = Path(__file__).resolve().parent / "config"
LEDGER_PATH = Path(__file__).resolve().parent / "ledger" / "events.jsonl"


def ensure_ledger() -> Sequencer:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    return Sequencer(str(LEDGER_PATH))


def cmd_run(args: argparse.Namespace) -> int:
    seq = ensure_ledger()
    seq.start()
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    seq.propose(task_id, "task.created", goal=args.requirement)
    # Stage A proof: record that we *can* invoke a vendor through the adapter.
    decls = load_vendors(CONFIG_DIR)
    reviewer = decls.get(args.vendor, decls["claude"])
    res = invoke(
        reviewer,
        f"Review task: {args.requirement}",
        schema={"type": "object", "properties": {"notes": {"type": "string"}}},
        dry_run=args.dry_run,
    )
    seq.propose(task_id, "agent.invoked", vendor=args.vendor, dry_run=args.dry_run)
    if not args.dry_run and res.get("returncode", 0) != 0:
        seq.propose(task_id, "agent.error", detail=res.get("stderr", "")[:500])
    seq.stop()
    print(f"task {task_id} recorded. ledger={LEDGER_PATH}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load()
    print(f"events in ledger: {len(events)}")
    for ev in events[-20:]:
        print(f"  {ev.get('event_id')} {ev.get('type')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="super-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("requirement")
    r.add_argument("--vendor", default="claude")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)
    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
