#!/usr/bin/env python
"""CLI entry point for the super-agent harness.

Stage A: ledger + vendor adapter (run/status).
Stage 0: review/log/show — drive the verification pipeline and inspect the ledger.

Usage:
  python -m harness.cli run "<requirement>" [--vendor claude|codex|agy] [--dry-run]
  python -m harness.cli review <dir> [--accept pytest tests/] [--reviewer codex] [--dry-run]
  python -m harness.cli status
  python -m harness.cli log <task>
  python -m harness.cli show design|plan
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from harness.core.invoke import invoke, load_vendors
from harness.core.ledger import Ledger, Sequencer
from harness.roles.review_flow import run_pipeline
from harness.roles.architect import propose as architect_propose
from harness.roles.decomposer import decompose as decomposer_decompose

CONFIG_DIR = Path(__file__).resolve().parent / "config"
LEDGER_PATH = Path(__file__).resolve().parent / "ledger" / "events.jsonl"
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"


def ensure_ledger() -> Sequencer:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    return Sequencer(str(LEDGER_PATH))


def cmd_decompose(args: argparse.Namespace) -> int:
    """Stage 2 (§9 ②): decompose a requirement or an architect design file
    into a structurally-checked task DAG.

    With --spec <file>: consume the design file produced by `architect`
    (requirement is recovered from its '# 設計: <req>' header). Otherwise the
    positional requirement is used (architect may be skipped).
    """
    spec_text = ""
    if args.spec:
        p = Path(args.spec)
        if not p.exists():
            print(json.dumps({"ok": False, "error": f"spec file not found: {args.spec}"},
                             ensure_ascii=False, indent=2))
            return 1
        spec_text = p.read_text(encoding="utf-8", errors="ignore")
    # recover requirement from the design file header if not given positionally
    requirement = args.requirement or ""
    if not requirement and spec_text:
        for line in spec_text.splitlines():
            if line.startswith("# 設計:"):
                requirement = line[len("# 設計:"):].strip()
                break
    if not requirement:
        print(json.dumps({"ok": False, "error": "requirement or --spec is required"},
                         ensure_ascii=False, indent=2))
        return 1

    seq = ensure_ledger()
    seq.start()
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    seq.propose(task_id, "task.created", goal=requirement, role="decomposer",
                design_ref=args.spec or "")
    out = decomposer_decompose(
        task_id,
        requirement,
        vendor=args.vendor,
        existing_design=spec_text,
        dry_run=args.dry_run,
        seq=seq,
    )
    seq.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_architect(args: argparse.Namespace) -> int:
    """Stage 1 (§9 ①): record design decisions as ADRs on the ledger.

    With --spec <file>: record the human-supplied design verbatim.
    Without: ask a read-only vendor to propose ADRs (or just dry-run the prompt).
    """
    seq = ensure_ledger()
    seq.start()
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    seq.propose(task_id, "task.created", goal=args.requirement, role="architect")
    adr = architect_propose(
        task_id,
        args.requirement,
        args.vendor,
        spec_path=args.spec,
        dry_run=args.dry_run,
        seq=seq,
    )
    seq.stop()
    print(json.dumps(adr, ensure_ascii=False, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    seq = ensure_ledger()
    seq.start()
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    seq.propose(task_id, "task.created", goal=args.requirement)
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


def cmd_review(args: argparse.Namespace) -> int:
    """Run the verification pipeline (CVE -> brief -> review -> adjudicate)
    against a worktree / case directory. This is the '⑤⑥⑦⑨' part of §9."""
    target = Path(args.dir)
    if not target.exists():
        print(f"error: directory not found: {target}", file=sys.stderr)
        return 2
    # acceptance: default to pytest tests/ with expect_exit=0 (green)
    if args.accept:
        verb, *rest = args.accept.split()
        acceptance = [{"verb": verb, "args": rest, "expect_exit": args.expect_exit}]
    else:
        acceptance = [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}]

    seq = ensure_ledger()
    seq.start()
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    j = run_pipeline(
        task_id,
        target,
        acceptance,
        reviewer_vendor=args.reviewer,
        budget_tokens=args.budget,
        dry_run=args.dry_run,
        seq=seq,
    )
    seq.stop()
    print(json.dumps(j, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load()
    print(f"events in ledger: {len(events)}")
    for ev in events[-20:]:
        print(f"  {ev.get('event_id')} {ev.get('type')}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load()
    matched = [e for e in events if e.get("event_id", "").startswith(args.task)]
    if not matched:
        print(f"no events for task prefix '{args.task}'")
        return 0
    print(f"events for {args.task}: {len(matched)}")
    for ev in matched:
        extra = {k: v for k, v in ev.items() if k not in ("event_id", "type", "ts")}
        print(f"  {ev.get('event_id')} {ev.get('type')} {json.dumps(extra, ensure_ascii=False)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Read-only view of design/plan. L6 'show' operation (no ledger event)."""
    if args.what == "design":
        path = DOCS / "goals" / "design.md"
        print(f"# 設計のゴールと評価ルーブリック\n# {path}\n")
        print(path.read_text(encoding="utf-8", errors="ignore")[:2000])
    elif args.what == "plan":
        path = DOCS / "plan.md"
        print(f"# 実装計画\n# {path}\n")
        print(path.read_text(encoding="utf-8", errors="ignore")[:2000])
    else:
        print(f"unknown show target: {args.what}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="super-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="record a requirement + invoke vendor (Stage A)")
    r.add_argument("requirement")
    r.add_argument("--vendor", default="claude")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("architect", help="record design decisions as ADRs (Stage 1)")
    a.add_argument("requirement")
    a.add_argument("--spec", default=None, help="human-supplied design file (recorded verbatim)")
    a.add_argument("--vendor", default="claude")
    a.add_argument("--dry-run", action="store_true",
                   help="assemble the architect prompt without calling the vendor")
    a.set_defaults(func=cmd_architect)

    d = sub.add_parser("decompose", help="decompose requirement/design into checked task DAG (Stage 2)")
    d.add_argument("requirement", nargs="?", default="",
                   help="requirement text (optional if --spec is given)")
    d.add_argument("--spec", default=None,
                   help="design file from `architect` (requirement recovered from its '# 設計:' header)")
    d.add_argument("--vendor", default="claude")
    d.add_argument("--dry-run", action="store_true",
                   help="assemble the decompose prompt without calling the vendor")
    d.set_defaults(func=cmd_decompose)

    rv = sub.add_parser("review", help="run verification pipeline on a dir (Stage 0)")
    rv.add_argument("dir", help="worktree / case directory")
    rv.add_argument("--accept", default=None,
                    help="acceptance as 'verb arg1 arg2 ...' (default: pytest tests/)")
    rv.add_argument("--expect-exit", dest="expect_exit", type=int, default=0)
    rv.add_argument("--reviewer", default="codex")
    rv.add_argument("--budget", type=int, default=4000)
    rv.add_argument("--dry-run", action="store_true",
                    help="run CVE but skip the live reviewer call")
    rv.set_defaults(func=cmd_review)

    s = sub.add_parser("status", help="show recent ledger events")
    s.set_defaults(func=cmd_status)

    l = sub.add_parser("log", help="show ledger events for a task prefix")
    l.add_argument("task", help="task id prefix, e.g. T-abc123")
    l.set_defaults(func=cmd_log)

    sh = sub.add_parser("show", help="read-only view of design/plan (L6)")
    sh.add_argument("what", choices=["design", "plan"])
    sh.set_defaults(func=cmd_show)

    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
