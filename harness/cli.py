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
from harness.roles.decomposer import render_tasks_md, parse_tasks_md, structural_check
from harness.roles.scheduler import schedule
from harness.roles.implementer import implement
from harness.core.verifiers import VerifierRegistry

CONFIG_DIR = Path(__file__).resolve().parent / "config"
LEDGER_PATH = Path(__file__).resolve().parent / "ledger" / "events.jsonl"
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"


def ensure_ledger() -> Sequencer:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    return Sequencer(str(LEDGER_PATH))


def cmd_plan(args: argparse.Namespace) -> int:
    """Stage 3 (§9 ③): decompose a requirement/design, then schedule worktrees + leases.

    Combines Stage 2 (decompose) and Stage 3 (scheduler) in one call.
    """
    spec_text = ""
    if args.spec:
        p = Path(args.spec)
        if not p.exists():
            print(json.dumps({"ok": False, "error": f"spec file not found: {args.spec}"},
                             ensure_ascii=False, indent=2))
            return 1
        spec_text = p.read_text(encoding="utf-8", errors="ignore")

    seq = ensure_ledger()
    seq.start()

    # --- tasks source: reuse --tasks file if it already exists (no vendor) ---
    tasks_file = Path(args.tasks) if args.tasks else None
    if tasks_file and tasks_file.exists():
        tasks = parse_tasks_md(str(tasks_file))
        requirement = args.requirement or ""
        if not requirement and spec_text:
            for line in spec_text.splitlines():
                if line.startswith("# 設計:"):
                    requirement = line[len("# 設計:"):].strip()
                    break
        # light structural validation so a hand-edited file still routes safely
        config_dir = Path(__file__).resolve().parent / "config"
        registry = VerifierRegistry(config_dir / "verifiers.yaml")
        errs = structural_check(tasks, registry)
        task_id = f"T-{uuid.uuid4().hex[:8]}"
        if errs:
            if seq is not None:
                seq.propose(task_id, "decompose.rejected", errors=errs)
            seq.stop()
            print(json.dumps({"ok": False, "errors": errs, "tasks": tasks},
                             ensure_ascii=False, indent=2))
            return 0
        out = {"ok": True, "tasks": tasks, "reused_tasks_file": True}
        if seq is not None:
            seq.propose(task_id, "decompose.ok", n_tasks=len(tasks),
                        source="tasks.md")
    else:
        # decompose via vendor (creates the task DAG)
        requirement = args.requirement or ""
        if not requirement and spec_text:
            for line in spec_text.splitlines():
                if line.startswith("# 設計:"):
                    requirement = line[len("# 設計:"):].strip()
                    break
        if not requirement:
            print(json.dumps({"ok": False, "error": "requirement or --spec is required"},
                             ensure_ascii=False, indent=2))
            seq.stop()
            return 1
        task_id = f"T-{uuid.uuid4().hex[:8]}"
        seq.propose(task_id, "task.created", goal=requirement, role="decomposer",
                    design_ref=args.spec or "")
        out = decomposer_decompose(
            task_id, requirement, vendor=args.vendor,
            existing_design=spec_text, dry_run=args.dry_run, seq=seq,
        )
        if not out.get("ok"):
            seq.stop()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

    sched = schedule(
        task_id, out.get("tasks", []), vendor=args.vendor,
        role="implementer", lease_seconds=args.lease, root=args.root,
        dry_run=args.dry_run, seq=seq,
    )
    seq.stop()

    if args.tasks and not args.dry_run and out.get("ok") and not out.get("reused_tasks_file"):
        Path(args.tasks).write_text(
            render_tasks_md(out.get("tasks", []), requirement), encoding="utf-8")
        print(f"# tasks written to {args.tasks}", file=sys.stderr)

    print(json.dumps({"decompose": out, "schedule": sched}, ensure_ascii=False, indent=2))
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


def cmd_implement(args: argparse.Namespace) -> int:
    """Stage 4 (§9 ④): implement a single task inside its worktree, then commit.

    Reads the task spec from --tasks, finds the task by --task, and runs the
    Implementer vendor inside workspaces/<task> (the worktree from `plan`).
    """
    tasks_file = Path(args.tasks) if args.tasks else None
    if not tasks_file or not tasks_file.exists():
        print(json.dumps({"ok": False, "error": f"tasks file not found: {args.tasks}"},
                         ensure_ascii=False, indent=2))
        return 1
    tasks = parse_tasks_md(str(tasks_file))
    task = next((t for t in tasks if t["task_id"] == args.task), None)
    if task is None:
        print(json.dumps({"ok": False, "error": f"task {args.task} not in {args.tasks}"},
                         ensure_ascii=False, indent=2))
        return 1

    worktree = args.worktree or str(Path("workspaces") / args.task)
    if not Path(worktree).exists():
        print(json.dumps({"ok": False, "error": f"worktree not found: {worktree} (run `plan` first)"},
                         ensure_ascii=False, indent=2))
        return 1

    seq = ensure_ledger()
    seq.start()
    out = implement(args.task, task, worktree, vendor=args.vendor,
                   seq=seq, dry_run=args.dry_run)
    seq.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
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

    pl = sub.add_parser("plan", help="decompose + schedule worktrees/leases (Stage 3)")
    pl.add_argument("requirement", nargs="?", default="",
                   help="requirement text (optional if --spec is given)")
    pl.add_argument("--spec", default=None,
                   help="design file from `architect` (requirement recovered from its '# 設計:' header)")
    pl.add_argument("--vendor", default="claude")
    pl.add_argument("--tasks", default=None,
                   help="write the decomposed task DAG as Markdown to this file")
    pl.add_argument("--lease", type=int, default=3600,
                   help="lease duration in seconds (default 3600)")
    pl.add_argument("--root", default="workspaces",
                   help="worktree root directory (default: workspaces)")
    pl.add_argument("--dry-run", action="store_true",
                   help="assemble prompts and plan worktrees without calling vendor / git")
    pl.set_defaults(func=cmd_plan)

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

    im = sub.add_parser("implement", help="implement a task in its worktree + commit (Stage 4)")
    im.add_argument("--task", required=True, help="task id to implement (e.g. T1)")
    im.add_argument("--tasks", default="probe/sample/my-design-tasks.md",
                    help="decomposed task DAG (to look up the task spec)")
    im.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    im.add_argument("--vendor", default="claude")
    im.add_argument("--dry-run", action="store_true",
                    help="assemble the implementer prompt without calling the vendor")
    im.set_defaults(func=cmd_implement)

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
