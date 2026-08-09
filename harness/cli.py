#!/usr/bin/env python
"""CLI entry point for the super-agent harness.

Stage A: ledger + vendor adapter (run/status).
Stage 0: review/log/show — drive the verification pipeline and inspect the ledger.

Usage:
  python -m harness.cli run "<requirement>" [--vendor claude|codex|agy|hermes] [--dry-run]
  python -m harness.cli review <dir> [--accept pytest tests/] [--reviewer codex|claude|agy|hermes] [--dry-run]
  python -m harness.cli status
  python -m harness.cli log <task>
  python -m harness.cli show design|plan
  python -m harness.cli dashboard [--format md|html|both] [--out <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from harness.core.invoke import invoke, load_vendors, resolve_role
from harness.core.ledger import Ledger, Sequencer
from harness.roles.review_flow import run_pipeline
from harness.roles.architect import propose as architect_propose
from harness.roles.decomposer import decompose as decomposer_decompose
from harness.roles.decomposer import render_tasks_md, parse_tasks_md, structural_check
from harness.roles.scheduler import schedule
from harness.roles.implementer import implement
from harness.roles.integrator import integrate
from harness.roles.drive import drive
from harness.roles.improver import mine as improver_mine, report as improver_report
from harness.core.verifiers import VerifierRegistry

CONFIG_DIR = Path(__file__).resolve().parent / "config"
# Ledger path can be overridden via SUPER_AGENT_LEDGER (used for sample/fixture
# ledgers without touching the real append-only events.jsonl).
LEDGER_PATH = Path(os.environ.get("SUPER_AGENT_LEDGER",
                                  str(Path(__file__).resolve().parent / "ledger" / "events.jsonl")))
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"


def ensure_worktree(task_id: str, root: str = "workspaces") -> str | None:
    """Return the worktree path for a task, recreating it from branch task/<id>
    if git metadata is stale (dir deleted but branch survives). Returns None if
    neither the dir nor a recoverable branch exists."""
    path = str(Path(root, task_id).as_posix())
    if Path(path).exists():
        return path
    branch = f"task/{task_id}"
    # prune stale 'registered but missing' metadata, then try to re-create
    subprocess.run(["git", "worktree", "prune"],
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace", shell=False)
    branches = subprocess.run(["git", "branch", "--list", branch],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", shell=False).stdout
    if branch in branches:
        r = subprocess.run(["git", "worktree", "add", path, branch],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", shell=False)
        if r.returncode == 0 and Path(path).exists():
            return path
    return None


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
            task_id, requirement,
            vendor=resolve_role("design", CONFIG_DIR, explicit_vendor=args.vendor,
                                explicit_model=getattr(args, "model", None),
                                explicit_effort=getattr(args, "effort", None))["vendor"],
            existing_design=spec_text, dry_run=args.dry_run,
            model=getattr(args, "model", None), seq=seq,
        )
        if not out.get("ok"):
            seq.stop()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

    sched = schedule(
        task_id, out.get("tasks", []),
        vendor=resolve_role("implement", CONFIG_DIR, explicit_vendor=args.vendor)["vendor"],
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
    r = resolve_role("design", CONFIG_DIR,
                     explicit_vendor=args.vendor,
                     explicit_model=getattr(args, "model", None),
                     explicit_effort=getattr(args, "effort", None))
    adr = architect_propose(
        task_id,
        args.requirement,
        r["vendor"],
        spec_path=args.spec,
        dry_run=args.dry_run,
        model=r["model"],
        effort=r["effort"],
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
    r = resolve_role("review", CONFIG_DIR, explicit_vendor=args.vendor,
                     explicit_model=getattr(args, "model", None),
                     explicit_effort=getattr(args, "effort", None))
    reviewer = decls.get(r["vendor"], decls["claude"])
    res = invoke(
        reviewer,
        f"Review task: {args.requirement}",
        schema={"type": "object", "properties": {"notes": {"type": "string"}}},
        model=r["model"], effort=r["effort"], dry_run=args.dry_run,
    )
    seq.propose(task_id, "agent.invoked", vendor=r["vendor"], dry_run=args.dry_run)
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

    # Resolve to an absolute path. The implementer prompt advertises
    # `worktree` as an ABSOLUTE path and instructs the vendor to write to
    # `<worktree>/...`. A relative default would make cwd-relative vendors
    # (hermes runs inside the worktree via subprocess cwd) nest the output
    # under <worktree>/<worktree>/..., so `git -C <worktree> add` finds
    # nothing. drive.py already resolves to absolute; keep this in sync.
    worktree = args.worktree or str((Path("workspaces") / args.task).resolve())
    if not Path(worktree).exists():
        # recover a stale/missing worktree from its branch if possible
        recovered = ensure_worktree(args.task, str(Path("workspaces")))
        if recovered:
            worktree = recovered
    if not Path(worktree).exists():
        print(json.dumps({"ok": False, "error": f"worktree not found: {worktree} (run `plan` first)"},
                         ensure_ascii=False, indent=2))
        return 1

    seq = ensure_ledger()
    seq.start()
    r = resolve_role("implement", CONFIG_DIR,
                     explicit_vendor=args.vendor,
                     explicit_model=getattr(args, "model", None),
                     explicit_effort=getattr(args, "effort", None))
    out = implement(args.task, task, worktree, vendor=r["vendor"],
                   seq=seq, dry_run=args.dry_run, model=r["model"], effort=r["effort"])
    seq.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    """Stage 5 (plan.md): merge an implemented task's worktree into the target
    branch, re-verify acceptance, then tear down the worktree.

    Resolves the task spec from --tasks, finds the worktree from --worktree or
    workspaces/<task>, and merges branch task/<task> into --target.
    """
    tasks_file = Path(args.tasks)
    if not tasks_file.exists():
        print(f"error: tasks file not found: {args.tasks}", file=sys.stderr)
        return 2
    tasks = parse_tasks_md(str(tasks_file))
    task = next((t for t in tasks if t["task_id"] == args.task), None)
    if task is None:
        print(f"error: task {args.task} not in {args.tasks}", file=sys.stderr)
        return 2

    worktree = args.worktree or str(Path("workspaces") / args.task)
    if not Path(worktree).exists():
        recovered = ensure_worktree(args.task, str(Path("workspaces")))
        if recovered:
            worktree = recovered
    if not Path(worktree).exists():
        print(json.dumps({"ok": False, "error": f"worktree not found: {worktree}"},
                         ensure_ascii=False, indent=2))
        return 1

    seq = ensure_ledger()
    seq.start()
    out = integrate(
        args.task, task, worktree,
        target_branch=args.target, seq=seq, dry_run=args.dry_run,
    )
    seq.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_drive(args: argparse.Namespace) -> int:
    """Stage B: drive the full implement->review->integrate pipeline for every
    task in the DAG.

    Default: each task is implemented in a single channel (no speculative
    racing); independent tasks run concurrently (task-level parallelism).
    Pass --speculative to fan each task out across multiple channels and
    integrate only the first reviewer-approved one.

    If --tasks does not exist, decompose from --spec first (creating worktrees).

    Both --spec (design_file) and --tasks (task_file) are REQUIRED: driving a
    task needs its design + task root to exist.
    """
    if not args.spec:
        print(json.dumps({"ok": False, "error": "--spec (design_file) is required"},
                         ensure_ascii=False, indent=2))
        return 1
    if not args.tasks:
        print(json.dumps({"ok": False, "error": "--tasks (task_file) is required"},
                         ensure_ascii=False, indent=2))
        return 1
    seq = ensure_ledger()
    seq.start()
    implement_channels = None
    if args.implement_vendors:
        from harness.core.invoke import parse_channel_override
        try:
            implement_channels = parse_channel_override(args.implement_vendors)
        except ValueError as e:
            print(f"error: --implement-vendors: {e}", file=sys.stderr)
            return 2
    # --speculative is implied when multiple channels are explicitly requested
    speculative = bool(getattr(args, "speculative", False)) or (
        implement_channels is not None and len(implement_channels) > 1
    )
    out = drive(
        requirement=args.requirement or "",
        spec_path=args.spec,
        tasks_path=args.tasks,
        target_branch=args.target,
        seq=seq,
        dry_run=args.dry_run,
        implement_vendor=args.vendor,
        reviewer_vendor=args.reviewer,
        implement_channels=implement_channels,
        parallel_tasks=args.parallel_tasks,
        max_task_workers=args.max_task_workers,
        speculative=speculative,
        adaptive=getattr(args, "adaptive", True),
        implement_model=args.model,
        implement_effort=args.effort,
        task_file=str(Path(args.tasks).resolve()),
    )
    seq.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not out.get("ok"):
        return 1
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Run the verification pipeline (CVE -> brief -> review -> adjudicate)
    against a worktree / case directory. This is the '⑤⑥⑦⑨' part of §9.

    With --task <id> --tasks <dag.md>, the acceptance and worktree path are
    resolved from the implemented task (Stage 4 -> Stage 5 handoff).
    """
    # Stage 5 handoff: resolve acceptance + worktree from the task DAG
    if getattr(args, "task", None) and getattr(args, "tasks", None):
        tasks_file = Path(args.tasks)
        if not tasks_file.exists():
            print(f"error: tasks file not found: {args.tasks}", file=sys.stderr)
            return 2
        tasks = parse_tasks_md(str(tasks_file))
        task = next((t for t in tasks if t["task_id"] == args.task), None)
        if task is None:
            print(f"error: task {args.task} not in {args.tasks}", file=sys.stderr)
            return 2
        target = Path(args.worktree) if args.worktree else (Path("workspaces") / args.task)
        # recover a stale/missing worktree from its branch before reviewing
        resolved = ensure_worktree(args.task, str(Path("workspaces")))
        if resolved:
            target = Path(resolved)
        acceptance = task.get("acceptance") or [
            {"verb": "pytest", "args": ["tests/"], "expect_exit": 0}]
    else:
        target = Path(getattr(args, "dir"))
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
    task_id = getattr(args, "task", None) or f"T-{uuid.uuid4().hex[:8]}"
    r = resolve_role("review", CONFIG_DIR,
                     explicit_vendor=args.reviewer,
                     explicit_model=getattr(args, "model", None),
                     explicit_effort=getattr(args, "effort", None))
    j = run_pipeline(
        task_id,
        target,
        acceptance,
        reviewer_vendor=r["vendor"],
        budget_tokens=args.budget,
        dry_run=args.dry_run,
        model=r["model"],
        effort=r["effort"],
        seq=seq,
    )
    seq.stop()
    print(json.dumps(j, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load_flat()
    print(f"events in ledger: {len(events)}")
    for ev in events[-20:]:
        print(f"  {ev.get('event_id')} {ev.get('type')}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load_flat()
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


def cmd_evolve(args: argparse.Namespace) -> int:
    """Stage 6 (§9 ⑩): mine the ledger for recurring failures and propose
    self-improvements. With --dry-run, only prints proposals; otherwise also
    records a ``design.proposed`` event and appends to the target file."""
    result = improver_mine(dry_run=args.dry_run)
    print(improver_report(result))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Generate dashboard (md, html, or both) from ledger events."""
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load_flat()

    # The model builder + renderers live in harness.roles.dashboard (this is the
    # single source of truth; no inline fallbacks — if the import fails the CLI
    # surfaces the error instead of silently diverging from the role module).
    from harness.roles.dashboard import build_model, render_markdown, render_html

    model = build_model(events)
    fmt = args.format
    out_path = getattr(args, "out", None) or getattr(args, "out_dir", None)

    md_content = render_markdown(model)
    html_content = render_html(model)

    if out_path:
        p = Path(out_path)
        if fmt == "md":
            if p.is_dir():
                (p / "dashboard.md").write_text(md_content, encoding="utf-8")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(md_content, encoding="utf-8")
        elif fmt == "html":
            if p.is_dir():
                (p / "dashboard.html").write_text(html_content, encoding="utf-8")
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(html_content, encoding="utf-8")
        elif fmt == "both":
            if p.is_dir() or not p.suffix:
                p.mkdir(parents=True, exist_ok=True)
                (p / "dashboard.md").write_text(md_content, encoding="utf-8")
                (p / "dashboard.html").write_text(html_content, encoding="utf-8")
            else:
                base = p.with_suffix("") if p.suffix in (".md", ".html") else p
                base.parent.mkdir(parents=True, exist_ok=True)
                Path(f"{base}.md").write_text(md_content, encoding="utf-8")
                Path(f"{base}.html").write_text(html_content, encoding="utf-8")
    else:
        if fmt == "md":
            sys.stdout.write(md_content)
        elif fmt == "html":
            sys.stdout.write(html_content)
        elif fmt == "both":
            sys.stdout.write(md_content)
            sys.stdout.write(html_content)

    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles/pipes default to the system locale codepage (e.g. cp932),
    # which can't encode arbitrary unicode pulled from design docs (em-dashes,
    # etc). Keep the encoding as-is (so callers decoding our output with the
    # same locale default still work) but replace unencodable characters
    # instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="super-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="record a requirement + invoke vendor (Stage A)")
    r.add_argument("requirement")
    r.add_argument("--vendor", default=None)
    r.add_argument("--model", default=None, help="override the vendor's default model")
    r.add_argument("--effort", default=None, help="override the vendor's default effort")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("architect", help="record design decisions as ADRs (Stage 1)")
    a.add_argument("requirement")
    a.add_argument("--spec", default=None, help="human-supplied design file (recorded verbatim)")
    a.add_argument("--vendor", default=None)
    a.add_argument("--model", default=None, help="override the vendor's default model")
    a.add_argument("--effort", default=None, help="override the vendor's default effort")
    a.add_argument("--dry-run", action="store_true",
                   help="assemble the architect prompt without calling the vendor")
    a.set_defaults(func=cmd_architect)

    pl = sub.add_parser("plan", help="decompose + schedule worktrees/leases (Stage 3)")
    pl.add_argument("requirement", nargs="?", default="",
                   help="requirement text (optional if --spec is given)")
    pl.add_argument("--spec", default=None,
                   help="design file from `architect` (requirement recovered from its '# 設計:' header)")
    pl.add_argument("--vendor", default=None)
    pl.add_argument("--model", default=None, help="override the vendor's default model")
    pl.add_argument("--effort", default=None, help="override the vendor's default effort")
    pl.add_argument("--tasks", default=None,
                   help="write the decomposed task DAG as Markdown to this file")
    pl.add_argument("--lease", type=int, default=3600,
                   help="lease duration in seconds (default 3600)")
    pl.add_argument("--root", default="workspaces",
                   help="worktree root directory (default: workspaces)")
    pl.add_argument("--dry-run", action="store_true",
                   help="assemble prompts and plan worktrees without calling vendor / git")
    pl.set_defaults(func=cmd_plan)

    rv = sub.add_parser("review", help="run verification pipeline on a dir (Stage 0/5)")
    rv.add_argument("dir", help="worktree / case directory to verify")
    rv.add_argument("--accept", default=None,
                    help="acceptance as 'verb arg1 arg2 ...' (default: pytest tests/)")
    rv.add_argument("--expect-exit", dest="expect_exit", type=int, default=0)
    rv.add_argument("--reviewer", default=None)
    rv.add_argument("--model", default=None, help="override the reviewer vendor's default model")
    rv.add_argument("--effort", default=None, help="override the reviewer vendor's default effort")
    rv.add_argument("--budget", type=int, default=4000)
    rv.add_argument("--dry-run", action="store_true",
                    help="run CVE but skip the live reviewer call")
    rv.set_defaults(func=cmd_review)

    rt = sub.add_parser("review-task", help="review an implemented task (Stage 5 handoff)")
    rt.add_argument("--task", required=True, help="task id to review (Stage 5 handoff from implement)")
    rt.add_argument("--tasks", default="probe/sample/my-design-tasks.md",
                    help="decomposed task DAG (to resolve --task's acceptance + worktree)")
    rt.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    rt.add_argument("--reviewer", default=None)
    rt.add_argument("--model", default=None, help="override the reviewer vendor's default model")
    rt.add_argument("--effort", default=None, help="override the reviewer vendor's default effort")
    rt.add_argument("--budget", type=int, default=4000)
    rt.add_argument("--dry-run", action="store_true",
                    help="run CVE but skip the live reviewer call")
    rt.set_defaults(func=cmd_review)

    im = sub.add_parser("implement", help="implement a task in its worktree + commit (Stage 4)")
    im.add_argument("--task", required=True, help="task id to implement (e.g. T1)")
    im.add_argument("--tasks", default="probe/sample/my-design-tasks.md",
                    help="decomposed task DAG (to look up the task spec)")
    im.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    im.add_argument("--vendor", default=None)
    im.add_argument("--model", default=None, help="override the implementer vendor's default model")
    im.add_argument("--effort", default=None, help="override the implementer vendor's default effort")
    im.add_argument("--dry-run", action="store_true",
                    help="assemble the implementer prompt without calling the vendor")
    im.set_defaults(func=cmd_implement)

    ig = sub.add_parser("integrate", help="merge implemented task into target + tear down (Stage 5)")
    ig.add_argument("--task", required=True, help="task id to integrate (e.g. T1)")
    ig.add_argument("--tasks", default="probe/sample/my-design-tasks.md",
                    help="decomposed task DAG (to look up the task spec)")
    ig.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    ig.add_argument("--target", default="main",
                    help="integration target branch (default: main)")
    ig.add_argument("--dry-run", action="store_true",
                    help="show the merge/verify plan without touching git")
    ig.set_defaults(func=cmd_integrate)

    dr = sub.add_parser("drive", help="drive implement->review->integrate for every task in the DAG (Stage B)")
    dr.add_argument("--requirement", default="", help="requirement text (used when --tasks is missing)")
    dr.add_argument("--spec", default=None, help="design file to decompose from when --tasks is missing")
    dr.add_argument("--tasks", default="probe/sample/my-design-tasks.md",
                    help="decomposed task DAG (created from --spec if missing)")
    dr.add_argument("--target", default="main",
                    help="integration target branch (default: main)")
    dr.add_argument("--vendor", default=None, help="implementer vendor (default: roles.implement)")
    dr.add_argument("--reviewer", default=None, help="reviewer vendor (default: roles.review)")
    dr.add_argument("--model", default=None,
                    help="override the implementer model (and all implement channels); "
                         "e.g. tencent/hy3:free. Normalized if a known alias is used.")
    dr.add_argument("--effort", default=None,
                    help="override the implementer effort (and all implement channels)")
    dr.add_argument("--implement-vendors", default=None,
                    help='multi-channel override, e.g. "agy:2,hermes:3" '
                         '(each entry becomes one parallel implement channel)')
    dr.add_argument("--parallel-tasks", action="store_true", default=True,
                    help="run independent tasks (topo layers) concurrently during "
                         "implement+review (integrate stays serial). "
                         "NOTE: task-level parallelism is ON by default; this flag "
                         "is accepted for explicitness but has no extra effect.")
    dr.add_argument("--speculative", action="store_true",
                    help="speculative multi-channel mode: fan each task out across "
                         "all declared implement channels (roles.implement) and "
                         "integrate only the first reviewer-approved one. Off by "
                         "default (each task implemented in a single channel).")
    dr.add_argument("--adaptive", action="store_true", default=True,
                    help="adaptive re-planning (planner role): between topo layers, "
                         "re-examine the DAG against ledger events, carve out "
                         "investigation tasks, and merge/re-order over-split tasks. "
                         "On by default. Use --no-adaptive to stick to the static DAG.")
    dr.add_argument("--no-adaptive", dest="adaptive", action="store_false",
                    help="disable adaptive re-planning (use the initial static DAG).")
    dr.add_argument("--max-task-workers", type=int, default=4,
                    help="max concurrent tasks (used when tasks are independent)")
    dr.add_argument("--dry-run", action="store_true",
                    help="assemble plans and run CVE, but skip vendor calls and git changes")
    dr.set_defaults(func=cmd_drive)

    s = sub.add_parser("status", help="show recent ledger events")
    s.set_defaults(func=cmd_status)

    l = sub.add_parser("log", help="show ledger events for a task prefix")
    l.add_argument("task", help="task id prefix, e.g. T-abc123")
    l.set_defaults(func=cmd_log)

    sh = sub.add_parser("show", help="read-only view of design/plan (L6)")
    sh.add_argument("what", choices=["design", "plan"])
    sh.set_defaults(func=cmd_show)

    ev = sub.add_parser("evolve", help="mine ledger for recurring failures and propose self-improvements (Stage 6)")
    ev.add_argument("--dry-run", action="store_true",
                    help="show proposed upgrades without recording them to the ledger")
    ev.set_defaults(func=cmd_evolve)

    db = sub.add_parser("dashboard", help="generate dashboard (md/html/both)")
    db.add_argument("--format", choices=["md", "html", "both"], default="md",
                    help="output format: md, html, or both (default: md)")
    db.add_argument("--out", "--out-dir", "--output", "-o", dest="out", default=None,
                    help="output file or directory path")
    db.set_defaults(func=cmd_dashboard)

    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
