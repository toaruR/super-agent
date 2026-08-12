#!/usr/bin/env python
"""CLI entry point for the super-agent harness.

Stage A: ledger + vendor adapter (run/status).
Stage 0: review/log — drive the verification pipeline and inspect the ledger.

Usage:
  python -m harness.cli run "<requirement>" [--vendor claude|codex|agy|hermes] [--dry-run]
  python -m harness.cli review <dir> [--accept pytest tests/] [--reviewer codex|claude|agy|hermes] [--dry-run]
  python -m harness.cli status
  python -m harness.cli log <task>
  python -m harness.cli dashboard [--format md|html|both] [--out <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from harness.core.invoke import (
    resolve_role, load_path_defaults, slugify, unique_path,
    default_task_path, latest_task_file, git_executable,
)
from harness.core.ledger import Ledger, Sequencer, _same_path
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
# design.md / tasks.md のデフォルト出力先（harness/config/paths.yaml）。
# --design_file / --task_file を省略したとき、architect/plan 系コマンドがここへ書き出す。
PATH_DEFAULTS = load_path_defaults(CONFIG_DIR)
# Ledger path can be overridden via SUPER_AGENT_LEDGER (used for sample/fixture
# ledgers without touching the real append-only events.jsonl).
LEDGER_PATH = Path(os.environ.get("SUPER_AGENT_LEDGER",
                                  str(Path(__file__).resolve().parent / "ledger" / "events.jsonl")))


def ensure_worktree(task_id: str, root: str = "workspaces") -> str | None:
    """Return the worktree path for a task, recreating it from branch task/<id>
    if git metadata is stale (dir deleted but branch survives). Returns None if
    neither the dir nor a recoverable branch exists."""
    path = str(Path(root, task_id).as_posix())
    if Path(path).exists():
        return path
    branch = f"task/{task_id}"
    git_bin = git_executable()
    try:
        # prune stale 'registered but missing' metadata, then try to re-create
        subprocess.run([git_bin, "worktree", "prune"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", shell=False)
        branches = subprocess.run([git_bin, "branch", "--list", branch],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", shell=False).stdout or ""
        if branch in branches:
            r = subprocess.run([git_bin, "worktree", "add", path, branch],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", shell=False)
            if r.returncode == 0 and Path(path).exists():
                return path
    except FileNotFoundError:
        pass
    return None


def ensure_ledger() -> Sequencer:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    return Sequencer(str(LEDGER_PATH))


def resolve_task_file_arg(task_file_arg: str | None) -> str | None:
    """--task_file fallback for read-only consumer commands (implement/integrate/
    review-task): if omitted, use the most recently written task file, either
    under `<design_dir>/*_tasks/` (new layout) or paths.yaml's tasks_dir
    (legacy flat layout). Returns None if none can be found (caller reports
    the "not found" error using whatever string it prefers)."""
    if task_file_arg:
        return task_file_arg
    fallback = latest_task_file(PATH_DEFAULTS["design_dir"], PATH_DEFAULTS["tasks_dir"])
    return str(fallback) if fallback else None


def resolve_design_file_arg(args: argparse.Namespace, seq: Sequencer) -> str | None:
    """Resolve the --design_file path from CLI args.

    Precedence:
    1. --design_file if explicitly given.
    2. If --design_file is omitted but --task_file points at an existing file, look up
       its design_file in the ledger (Sequencer.resolve_design_file — the
       reverse of resolve_task_file).
    3. Infer design_file from task_file path convention (<design_stem>_tasks/<slug>.md -> <design_stem>.md).
    4. None if neither yields anything.
    """
    design_file = getattr(args, "design_file", None)
    if design_file:
        return design_file
    task_file = getattr(args, "task_file", None)
    if task_file:
        tf = Path(task_file)
        if tf.exists():
            found = seq.resolve_design_file(str(tf.resolve()))
            if found:
                return found
        parent = tf.parent
        if parent.name.endswith("_tasks"):
            stem = parent.name[:-len("_tasks")]
            candidate = parent.parent / f"{stem}.md"
            if candidate.exists():
                return str(candidate)
    return None


def check_task_file(tasks_path: str, design_path: str | None, seq: Sequencer,
                    *, auto_named: bool) -> str | None:
    """Guard the --task_file path before any write happens. Returns an error
    string, or None if the path is fine to proceed with.

    Guard A (auto_named=True): the caller computed `tasks_path` itself (user
    omitted --task_file). If a file already sits at that exact auto-named path,
    error out rather than silently reusing it or picking a `-2` suffix (the
    task-file equivalent of `unique_path()`'s collision-avoidance is
    intentionally NOT used here).

    Guard B (auto_named=False): the user explicitly pointed --task_file at an
    existing file. If the ledger already has a design_file recorded for that
    task_file, and it doesn't match `design_path` (path-normalized
    comparison), error out — reusing a task file across two different
    designs would silently merge unrelated task DAGs into the ledger.
    """
    p = Path(tasks_path)
    if auto_named:
        if p.exists():
            return (f"task file already exists at the auto-named path {tasks_path}; "
                     "pass --task_file explicitly to reuse it, or remove it first")
        return None
    if p.exists():
        recorded = seq.resolve_design_file(str(p.resolve()))
        if recorded and design_path and not _same_path(recorded, design_path):
            return (f"--task_file {tasks_path} is already registered in the ledger under "
                     f"design_file={recorded!r}, which does not match this design_file="
                     f"{design_path!r}")
    return None


def cmd_plan(args: argparse.Namespace) -> int:
    """Stage 3 (§9 ③): decompose a requirement/design, then schedule worktrees + leases.

    Combines Stage 2 (decompose) and Stage 3 (scheduler) in one call.

    --design_file is required (either explicitly, or recoverable from the ledger via
    --task_file pointing at an already-registered file — see resolve_design_file_arg()).
    Task files now live at <design_stem>_tasks/<slug>.md, next to the design
    file, so plan needs a design_file settled before it can pick a default
    --task_file path.
    """
    seq = ensure_ledger()

    args.design_file = resolve_design_file_arg(args, seq)
    if not args.design_file:
        print(json.dumps({"ok": False,
                          "error": "cannot determine design_file: pass --design_file explicitly, "
                                   "or point --task_file at a file already registered in the ledger"},
                         ensure_ascii=False, indent=2))
        return 1

    # --design_file is read-only input here (plan never writes a design file). Given:
    # must exist (typo protection).
    p = Path(args.design_file)
    if not p.exists():
        print(json.dumps({"ok": False, "error": f"design_file not found: {args.design_file}"},
                         ensure_ascii=False, indent=2))
        return 1
    spec_text = p.read_text(encoding="utf-8", errors="ignore")

    requirement = args.requirement or ""
    if not requirement and spec_text:
        for line in spec_text.splitlines():
            if line.startswith("# 設計:"):
                requirement = line[len("# 設計:"):].strip()
                break

    # --task_file omitted: auto-name a fresh file under <design_stem>_tasks/
    # (no collision-avoiding suffix — guard A below rejects an existing file
    # at this path instead of silently picking `-2`).
    auto_named = not args.task_file
    if auto_named:
        args.task_file = str(default_task_path(args.design_file, slugify(requirement or "tasks")))

    guard_err = check_task_file(args.task_file, args.design_file, seq, auto_named=auto_named)
    if guard_err:
        print(json.dumps({"ok": False, "error": guard_err}, ensure_ascii=False, indent=2))
        return 1

    seq.start()

    # --- tasks source: reuse --task_file file if it already exists (no vendor) ---
    tasks_file = Path(args.task_file)
    if tasks_file.exists():
        tasks = parse_tasks_md(str(tasks_file))
        # light structural validation so a hand-edited file still routes safely
        config_dir = Path(__file__).resolve().parent / "config"
        registry = VerifierRegistry(config_dir / "verifiers.yaml")
        errs = structural_check(tasks, registry)
        task_id = f"T-{uuid.uuid4().hex[:8]}"
        if errs:
            seq.propose(task_id, "decompose.rejected", errors=errs,
                        design_file=args.design_file, task_file=str(tasks_file.resolve()))
            seq.stop()
            print(json.dumps({"ok": False, "errors": errs, "tasks": tasks},
                             ensure_ascii=False, indent=2))
            return 0
        out = {"ok": True, "tasks": tasks, "reused_tasks_file": True}
        seq.propose(task_id, "decompose.ok", n_tasks=len(tasks), source="tasks.md",
                    design_file=args.design_file, task_file=str(tasks_file.resolve()))
    else:
        # decompose via vendor (creates the task DAG)
        if not requirement:
            print(json.dumps({"ok": False, "error": "requirement or --design_file is required"},
                             ensure_ascii=False, indent=2))
            seq.stop()
            return 1
        task_id = f"T-{uuid.uuid4().hex[:8]}"
        seq.propose(task_id, "task.created", goal=requirement, role="decomposer",
                    design_file=args.design_file, task_file=str(tasks_file.resolve()))
        design_role = resolve_role("design", CONFIG_DIR, explicit_vendor=args.vendor,
                                   explicit_model=getattr(args, "model", None),
                                   explicit_effort=getattr(args, "effort", None),
                                   explicit_timeout=getattr(args, "timeout", None))
        out = decomposer_decompose(
            task_id, requirement,
            vendor=design_role["vendor"],
            existing_design=spec_text, dry_run=args.dry_run,
            model=design_role["model"], effort=design_role["effort"],
            seq=seq, design_file=args.design_file,
            timeout=design_role["timeout"],
            task_file=str(tasks_file.resolve()),
        )
        if not out.get("ok"):
            seq.stop()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

    sched = schedule(
        task_id, out.get("tasks", []),
        vendor=resolve_role("implement", CONFIG_DIR, explicit_vendor=args.vendor)["vendor"],
        role="implementer", lease_seconds=args.lease, root=args.root,
        dry_run=args.dry_run, seq=seq, design_file=args.design_file,
    )
    seq.stop()

    if args.task_file and not args.dry_run and out.get("ok") and not out.get("reused_tasks_file"):
        tasks_out = Path(args.task_file)
        tasks_out.parent.mkdir(parents=True, exist_ok=True)
        tasks_out.write_text(
            render_tasks_md(out.get("tasks", []), requirement), encoding="utf-8")
        print(f"# tasks written to {args.task_file}", file=sys.stderr)

    print(json.dumps({"decompose": out, "schedule": sched}, ensure_ascii=False, indent=2))
    return 0


def extract_requirement_from_text(text: str, default: str = "") -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return default
    first_line = lines[0]
    if first_line.startswith("# 設計:"):
        return first_line[len("# 設計:"):].strip()
    return first_line.lstrip("#").strip()


def cmd_architect(args: argparse.Namespace) -> int:
    """Stage 1 (§9 ①): record design decisions as ADRs on the ledger.

    With --design_file <file> that already exists:
    - If the file has a leading `# 設計:` marker, record human-supplied design verbatim.
    - If missing `# 設計:`, prepend `# 設計: <requirement>`, save as a new file under
      design_dir (leaving the original file untouched), and register the new path in the ledger.
    Without an existing --design_file file: requirement is required — a read-only
    vendor proposes ADRs, saved as a new, non-colliding file under
    paths.yaml's design_dir (or just dry-run the prompt).
    """
    spec_exists = args.design_file is not None and Path(args.design_file).exists()
    requirement = args.requirement

    if spec_exists:
        text = Path(args.design_file).read_text(encoding="utf-8", errors="ignore")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        has_header = len(lines) > 0 and lines[0].startswith("# 設計:")

        if not requirement:
            requirement = extract_requirement_from_text(text, default=Path(args.design_file).stem)

        if not has_header:
            completed_text = f"# 設計: {requirement}\n\n{text}"
            new_design_path = unique_path(PATH_DEFAULTS["design_dir"], slugify(requirement or "design"))
            new_design_path.parent.mkdir(parents=True, exist_ok=True)
            new_design_path.write_text(completed_text, encoding="utf-8")
            args.design_file = str(new_design_path)
            print(f"# completed design written to {args.design_file}", file=sys.stderr)

    if not spec_exists and not requirement:
        print(json.dumps({"ok": False,
                          "error": "requirement is required unless --design_file points to an existing design file"},
                         ensure_ascii=False, indent=2))
        return 1
    if args.design_file is None:
        args.design_file = str(unique_path(PATH_DEFAULTS["design_dir"], slugify(requirement)))
    seq = ensure_ledger()
    seq.start()
    task_id = f"T-{uuid.uuid4().hex[:8]}"
    seq.propose(task_id, "task.created", goal=requirement, role="architect", design_file=args.design_file)
    r = resolve_role("design", CONFIG_DIR,
                     explicit_vendor=args.vendor,
                     explicit_model=getattr(args, "model", None),
                     explicit_effort=getattr(args, "effort", None),
                     explicit_timeout=args.timeout)
    adr = architect_propose(
        task_id,
        requirement,
        r["vendor"],
        spec_path=args.design_file,
        dry_run=args.dry_run,
        model=r["model"],
        effort=r["effort"],
        seq=seq,
        timeout=r["timeout"],
    )
    seq.stop()
    print(json.dumps(adr, ensure_ascii=False, indent=2))
    return 0


def cmd_implement(args: argparse.Namespace) -> int:
    """Stage 4 (§9 ④): implement a single task inside its worktree, then commit.

    Reads the task spec from --task_file, finds the task by --task, and runs the
    Implementer vendor inside workspaces/<task> (the worktree from `plan`).
    """
    seq = ensure_ledger()

    design_file = resolve_design_file_arg(args, seq)
    if design_file and not args.task_file:
        from harness.core.invoke import tasks_dir_for_design
        candidate_dir = tasks_dir_for_design(design_file)
        if candidate_dir.exists():
            matches = list(candidate_dir.glob("*.md"))
            if matches:
                args.task_file = str(matches[0])

    args.task_file = resolve_task_file_arg(args.task_file)
    if not design_file and args.task_file:
        design_file = resolve_design_file_arg(args, seq)

    tasks_file = Path(args.task_file) if args.task_file else None
    if not tasks_file or not tasks_file.exists():
        print(json.dumps({"ok": False, "error": f"tasks file not found: {args.task_file}"},
                         ensure_ascii=False, indent=2))
        return 1
    tasks = parse_tasks_md(str(tasks_file))
    task = next((t for t in tasks if t["task_id"] == args.task), None)
    if task is None:
        print(json.dumps({"ok": False, "error": f"task {args.task} not in {args.task_file}"},
                         ensure_ascii=False, indent=2))
        return 1

    from harness.roles.scheduler import effective_worktree_id, create_worktree
    eff_id = effective_worktree_id(args.task, design_file or "")
    worktree = args.worktree or str((Path("workspaces") / eff_id).resolve())
    if not Path(worktree).exists():
        # recover a stale/missing worktree from its branch if possible
        recovered = ensure_worktree(eff_id, str(Path("workspaces")))
        if recovered:
            worktree = recovered
    if not Path(worktree).exists():
        cw = create_worktree(args.task, root="workspaces", dry_run=args.dry_run, design_file=design_file or "")
        if cw.get("ok") and Path(cw["path"]).exists():
            worktree = str(Path(cw["path"]).resolve())
        else:
            print(json.dumps({"ok": False, "error": f"worktree not found and create failed: {worktree}"},
                             ensure_ascii=False, indent=2))
            return 1

    seq.start()
    auto_update_dashboard()
    r = resolve_role("implement", CONFIG_DIR,
                     explicit_vendor=args.vendor,
                     explicit_model=getattr(args, "model", None),
                     explicit_effort=getattr(args, "effort", None),
                     explicit_timeout=getattr(args, "timeout", None))
    out = implement(args.task, task, worktree, vendor=r["vendor"],
                   seq=seq, dry_run=args.dry_run, model=r["model"], effort=r["effort"],
                   design_file=design_file or "", timeout=r["timeout"])
    seq.stop()
    auto_update_dashboard()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    """Stage 5 (plan.md): merge an implemented task's worktree into the target
    branch, re-verify acceptance, then tear down the worktree.

    Resolves the task spec from --task_file, finds the worktree from --worktree or
    workspaces/<task>, and merges branch task/<task> into --target.
    """
    args.task_file = resolve_task_file_arg(args.task_file)
    tasks_file = Path(args.task_file) if args.task_file else None
    if not tasks_file or not tasks_file.exists():
        print(f"error: tasks file not found: {args.task_file}", file=sys.stderr)
        return 2
    tasks = parse_tasks_md(str(tasks_file))
    task = next((t for t in tasks if t["task_id"] == args.task), None)
    if task is None:
        print(f"error: task {args.task} not in {args.task_file}", file=sys.stderr)
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
        all_tasks=tasks,
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

    If --task_file does not exist, decompose from --design_file first (creating worktrees).

    Both --design_file and --task_file are required identifiers
    (driving a task needs its design + task root to exist as bookkeeping
    labels), but both are auto-named when omitted, so this never fails for
    that reason alone: --design_file falls back to resolve_design_file_arg() (ledger lookup via
    --task_file, else a freshly auto-named design_dir path), and --task_file falls
    back to <design_stem>_tasks/<slug>.md next to whatever --design_file resolved to.
    """
    seq = ensure_ledger()

    resolved_spec = resolve_design_file_arg(args, seq)
    if resolved_spec:
        args.design_file = resolved_spec
    elif not args.design_file:
        args.design_file = str(unique_path(PATH_DEFAULTS["design_dir"], slugify(args.requirement or "drive")))

    if not args.requirement and args.design_file and Path(args.design_file).exists():
        spec_text = Path(args.design_file).read_text(encoding="utf-8", errors="ignore")
        args.requirement = extract_requirement_from_text(spec_text, default=Path(args.design_file).stem)

    auto_named = not args.task_file
    if auto_named:
        args.task_file = str(default_task_path(args.design_file, slugify(args.requirement or "drive")))

    guard_err = check_task_file(args.task_file, args.design_file, seq, auto_named=auto_named)
    if guard_err:
        print(json.dumps({"ok": False, "error": guard_err}, ensure_ascii=False, indent=2))
        return 1

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
        spec_path=args.design_file,
        tasks_path=args.task_file,
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
        implement_timeout=args.timeout,
        task_file=str(Path(args.task_file).resolve()),
    )
    seq.stop()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not out.get("ok"):
        return 1
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Run the verification pipeline (CVE -> brief -> review -> adjudicate)
    against a worktree / case directory. This is the '⑤⑥⑦⑨' part of §9.

    With --task <id> --task_file <dag.md>, the acceptance and worktree path are
    resolved from the implemented task (Stage 4 -> Stage 5 handoff).
    """
    # Stage 5 handoff: resolve acceptance + worktree from the task DAG
    # (only `review-task` declares --task; plain `review <dir>` doesn't, so
    # getattr's None default routes it to the `else` branch below)
    if getattr(args, "task", None):
        args.task_file = resolve_task_file_arg(getattr(args, "task_file", None))
        tasks_file = Path(args.task_file) if args.task_file else None
        if not tasks_file or not tasks_file.exists():
            print(f"error: tasks file not found: {args.task_file}", file=sys.stderr)
            return 2
        tasks = parse_tasks_md(str(tasks_file))
        task = next((t for t in tasks if t["task_id"] == args.task), None)
        if task is None:
            print(f"error: task {args.task} not in {args.task_file}", file=sys.stderr)
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
                     explicit_effort=getattr(args, "effort", None),
                     explicit_timeout=getattr(args, "timeout", None))
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
        timeout=r["timeout"],
    )
    seq.stop()
    print(json.dumps(j, ensure_ascii=False, indent=2))
    return 0


# Lifecycle events that mark a meaningful transition in a task's progress.
# Used by cmd_status to render the "milestone log" (the progression-relevant
# subset of the ledger, not every low-level event).
_MILESTONE_TYPES = (
    "task.created",
    "task.scheduled",
    "task.leased",
    "task.implemented",
    "implement.ok",
    "artifact.produced",
    "review.pass",
    "review.fail",
    "integrate.ok",
    "integrate.error",
    "judgment",
)

# Statuses that count as "done" for the overall progress rate.
_DONE_STATUSES = ("integrated", "passed")


def cmd_status(args: argparse.Namespace) -> int:
    """Show an overall progress summary, the logical task list, and the
    milestone log derived from the ledger.

    Three sections (task requirement):
      1. progress summary  — overall completion rate across logical tasks
      2. logical tasks     — one line per task_id with its resolved status
      3. milestone log     — lifecycle-transition events in ledger order
    """
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load_flat()
    print(f"events in ledger: {len(events)}")

    # The dashboard role module is the single source of truth for the
    # task_id -> status aggregation (state-transition priority). Reuse it
    # rather than re-deriving status here so status semantics stay in sync.
    from harness.roles.dashboard import build_model, format_ts, group_by_design_file

    model = build_model(events)

    # --- 1) overall progress summary ---
    total = len(model)
    if total:
        status_counts: dict[str, int] = {}
        for info in model.values():
            status_counts[info["status"]] = status_counts.get(info["status"], 0) + 1
        done = sum(1 for info in model.values() if info["status"] in _DONE_STATUSES)
        rate = done / total * 100.0
        top = ", ".join(f"{s}={c}" for s, c in sorted(
            status_counts.items(), key=lambda kv: (-kv[1], kv[0])))
        design_files = sorted({info["design_file"] for info in model.values()
                                if info["design_file"]})
        print(f"\n# Progress summary")
        print(f"  logical tasks: {total}")
        print(f"  done ({'/'.join(_DONE_STATUSES)}): {done}")
        print(f"  overall progress: {done}/{total} ({rate:.1f}%)")
        print(f"  by status: {top}")
        if design_files:
            print(f"  design files: {', '.join(design_files)}")
    else:
        print(f"\n# Progress summary")
        print(f"  logical tasks: 0")
        print(f"  overall progress: 0/0 (0.0%)")

    # --- 2) logical task list (grouped by design_file) ---
    print(f"\n# Logical tasks")
    if model:
        for design_file, tasks in group_by_design_file(model).items():
            print(f"  ## {design_file}")
            for task_id, info in sorted(tasks.items()):
                created = format_ts(info["created_at"])
                updated = format_ts(info["updated_at"])
                print(f"    {task_id}\t{info['status']}"
                      f"\tcreated={created}\tupdated={updated}")
    else:
        print("  (no logical tasks recorded)")

    # --- 3) milestone log (lifecycle transitions, newest 20) ---
    print(f"\n# Milestone log")
    milestones = [e for e in events if e.get("type") in _MILESTONE_TYPES]
    if milestones:
        for ev in milestones[-20:]:
            extra = {k: v for k, v in ev.items()
                     if k not in ("event_id", "type", "ts",
                                  "design_file", "task_file")}
            print(f"  {ev.get('event_id')} {ev.get('type')} "
                  f"{json.dumps(extra, ensure_ascii=False)}")
    else:
        print("  (no milestones recorded)")

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


def cmd_evolve(args: argparse.Namespace) -> int:
    """Stage 6 (§9 ⑩): mine the ledger for recurring failures and propose
    self-improvements. With --dry-run, only prints proposals; otherwise also
    records a ``design.proposed`` event and appends to the target file."""
    result = improver_mine(dry_run=args.dry_run)
    print(improver_report(result))
    return 0


_DEFAULT_WATCH_INTERVAL = 5


def _render_dashboard_once(fmt: str, out_path: str | None,
                            refresh_interval: int | None = None) -> None:
    """Build the model from the live ledger/progress and write/print it once.

    ``refresh_interval``, if given, is embedded as a
    ``<meta http-equiv="refresh">`` tag in the HTML output so a browser
    viewing the file auto-reloads while ``--watch`` keeps regenerating it
    (docs/design/timeout-liveness-watchdog.md §5).
    """
    lg = Ledger(str(LEDGER_PATH))
    events = lg.load_flat()

    # The model builder + renderers live in harness.roles.dashboard (this is the
    # single source of truth; no inline fallbacks — if the import fails the CLI
    # surfaces the error instead of silently diverging from the role module).
    from harness.roles.dashboard import build_model, load_progress, render_markdown, render_html

    progress = load_progress(str(LEDGER_PATH))
    model = build_model(events, progress=progress)

    md_content = render_markdown(model)
    html_content = render_html(model, refresh_interval=refresh_interval)

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


def auto_update_dashboard() -> None:
    """Auto-refresh dashboard.html/dashboard.md files in the cwd or docs/ if present."""
    targets = [
        Path("dashboard.html"),
        Path("dashboard.md"),
        Path("docs/dashboard.html"),
        Path("docs/dashboard.md"),
    ]
    for target in targets:
        if target.exists():
            try:
                _render_dashboard_once("both", str(target.parent))
            except Exception:
                pass
            break


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Generate dashboard (md, html, or both) from ledger events.

    With ``--watch``, loops regenerating the output every ``--interval``
    seconds (default 5s) instead of running once — no server/websocket, just
    a plain re-render loop terminated by Ctrl+C
    (docs/design/timeout-liveness-watchdog.md §5).
    """
    fmt = args.format
    out_path = getattr(args, "out", None) or getattr(args, "out_dir", None)

    if not getattr(args, "watch", False):
        _render_dashboard_once(fmt, out_path)
        return 0

    interval = getattr(args, "interval", None) or _DEFAULT_WATCH_INTERVAL
    try:
        while True:
            _render_dashboard_once(fmt, out_path, refresh_interval=interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
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

    a = sub.add_parser("architect", help="record design decisions as ADRs (Stage 1)")
    a.add_argument("requirement", nargs="?", default="",
                   help="requirement text (optional if --design_file points to an existing design "
                        "file; recovered from the file's first line if omitted there)")
    a.add_argument("--design_file", default=None,
                   help="human-supplied design file (recorded verbatim). If it doesn't exist, "
                        "an LLM proposes it and it is saved as a new, non-colliding "
                        f"file under paths.yaml's design_dir ({PATH_DEFAULTS['design_dir']}).")
    a.add_argument("--vendor", default=None)
    a.add_argument("--model", default=None, help="override the vendor's default model")
    a.add_argument("--effort", default=None, help="override the vendor's default effort")
    a.add_argument("--dry-run", action="store_true",
                   help="assemble the architect prompt without calling the vendor")
    a.add_argument("--timeout", type=int, default=None,
                   help="seconds to wait for the vendor subprocess (default: 1800)")
    a.set_defaults(func=cmd_architect)

    pl = sub.add_parser("plan", help="decompose + schedule worktrees/leases (Stage 3)")
    pl.add_argument("requirement", nargs="?", default="",
                   help="requirement text (optional if --design_file is given)")
    pl.add_argument("--design_file", default=None,
                   help="design file from `architect` (requirement recovered from its '# 設計:' header). "
                        "Required: either pass it explicitly, or point --task_file at a file already "
                        "registered in the ledger (its design_file is looked up automatically). "
                        "`plan` no longer works with neither given.")
    pl.add_argument("--vendor", default=None)
    pl.add_argument("--model", default=None, help="override the vendor's default model")
    pl.add_argument("--effort", default=None, help="override the vendor's default effort")
    pl.add_argument("--task_file", default=None,
                   help="write the decomposed task DAG as Markdown to this file. If omitted, "
                        "auto-named (slug of the requirement) under <design_stem>_tasks/, next to "
                        "--design_file (no collision-avoiding suffix: an existing file at that path is an "
                        "error — pass --task_file explicitly to reuse it).")
    pl.add_argument("--lease", type=int, default=3600,
                   help="lease duration in seconds (default 3600)")
    pl.add_argument("--root", default="workspaces",
                   help="worktree root directory (default: workspaces)")
    pl.add_argument("--timeout", type=int, default=None,
                   help="seconds to wait for the vendor subprocess (default: 1800)")
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
    rv.add_argument("--timeout", type=int, default=None,
                    help="seconds to wait for the reviewer subprocess (default: 1800)")
    rv.add_argument("--dry-run", action="store_true",
                    help="run CVE but skip the live reviewer call")
    rv.set_defaults(func=cmd_review)

    rt = sub.add_parser("review-task", help="review an implemented task (Stage 5 handoff)")
    rt.add_argument("--task", required=True, help="task id to review (Stage 5 handoff from implement)")
    rt.add_argument("--design_file", default=None, help="design specification document path")
    rt.add_argument("--task_file", default=None,
                    help="decomposed task DAG (to resolve --task's acceptance + worktree). "
                         "If omitted, falls back to the most recently written task file, "
                         f"searching both <design_dir>/*_tasks/ ({PATH_DEFAULTS['design_dir']}) "
                         f"and paths.yaml's tasks_dir ({PATH_DEFAULTS['tasks_dir']}, legacy layout).")
    rt.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    rt.add_argument("--reviewer", default=None)
    rt.add_argument("--model", default=None, help="override the reviewer vendor's default model")
    rt.add_argument("--effort", default=None, help="override the reviewer vendor's default effort")
    rt.add_argument("--budget", type=int, default=4000)
    rt.add_argument("--timeout", type=int, default=None,
                    help="seconds to wait for the reviewer subprocess (default: 1800)")
    rt.add_argument("--dry-run", action="store_true",
                    help="run CVE but skip the live reviewer call")
    rt.set_defaults(func=cmd_review)

    im = sub.add_parser("implement", help="implement a task in its worktree + commit (Stage 4)")
    im.add_argument("--task", required=True, help="task id to implement (e.g. T1)")
    im.add_argument("--design_file", default=None, help="design specification document path")
    im.add_argument("--task_file", default=None,
                    help="decomposed task DAG (to look up the task spec). "
                         "If omitted, falls back to the most recently written task file, "
                         f"searching both <design_dir>/*_tasks/ ({PATH_DEFAULTS['design_dir']}) "
                         f"and paths.yaml's tasks_dir ({PATH_DEFAULTS['tasks_dir']}, legacy layout).")
    im.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    im.add_argument("--vendor", default=None)
    im.add_argument("--model", default=None, help="override the implementer vendor's default model")
    im.add_argument("--effort", default=None, help="override the implementer vendor's default effort")
    im.add_argument("--timeout", type=int, default=None,
                    help="seconds to wait for the vendor subprocess (default: 1800)")
    im.add_argument("--dry-run", action="store_true",
                    help="assemble the implementer prompt without calling the vendor")
    im.set_defaults(func=cmd_implement)

    ig = sub.add_parser("integrate", help="merge implemented task into target + tear down (Stage 5)")
    ig.add_argument("--task", required=True, help="task id to integrate (e.g. T1)")
    ig.add_argument("--design_file", default=None, help="design specification document path")
    ig.add_argument("--task_file", default=None,
                    help="decomposed task DAG (to look up the task spec). "
                         "If omitted, falls back to the most recently written task file, "
                         f"searching both <design_dir>/*_tasks/ ({PATH_DEFAULTS['design_dir']}) "
                         f"and paths.yaml's tasks_dir ({PATH_DEFAULTS['tasks_dir']}, legacy layout).")
    ig.add_argument("--worktree", default=None,
                    help="worktree path (default: workspaces/<task>)")
    ig.add_argument("--target", default="main",
                    help="integration target branch (default: main)")
    ig.add_argument("--dry-run", action="store_true",
                    help="show the merge/verify plan without touching git")
    ig.set_defaults(func=cmd_integrate)

    dr = sub.add_parser("drive", help="drive implement->review->integrate for every task in the DAG (Stage B)")
    dr.add_argument("--requirement", default="", help="requirement text (used when --task_file is missing)")
    dr.add_argument("--design_file", default=None,
                    help="design file to decompose from when --task_file is missing. If omitted, "
                         "resolved from the ledger when --task_file already points at a registered "
                         "file; otherwise auto-named (slug of --requirement, collision-avoided) "
                         f"under paths.yaml's design_dir ({PATH_DEFAULTS['design_dir']}).")
    dr.add_argument("--task_file", default=None,
                    help="decomposed task DAG (created from --design_file if missing). If omitted, "
                         "auto-named (slug of --requirement) under <design_stem>_tasks/, next to "
                         "--design_file (no collision-avoiding suffix: an existing file at that path is "
                         "an error unless --task_file is passed explicitly to reuse it).")
    dr.add_argument("--target", default=None,
                    help="integration target branch (default: derived from "
                         "--design_file as design/<stem>-<crc32>, so drive "
                         "never auto-creates a stray 'main'; falls back to "
                         "'main' if no design file is known)")
    dr.add_argument("--vendor", default=None, help="implementer vendor (default: roles.implement)")
    dr.add_argument("--reviewer", default=None, help="reviewer vendor (default: roles.review)")
    dr.add_argument("--model", default=None,
                    help="override the implementer model (and all implement channels); "
                         "e.g. tencent/hy3:free. Normalized if a known alias is used.")
    dr.add_argument("--effort", default=None,
                    help="override the implementer effort (and all implement channels)")
    dr.add_argument("--timeout", type=int, default=None,
                    help="override the implementer subprocess timeout in seconds "
                         "(and all implement channels; default: 1800)")
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

    ev = sub.add_parser("evolve", help="mine ledger for recurring failures and propose self-improvements (Stage 6)")
    ev.add_argument("--dry-run", action="store_true",
                    help="show proposed upgrades without recording them to the ledger")
    ev.set_defaults(func=cmd_evolve)

    db = sub.add_parser("dashboard", help="generate dashboard (md/html/both)")
    db.add_argument("--format", choices=["md", "html", "both"], default="md",
                    help="output format: md, html, or both (default: md)")
    db.add_argument("--out", "--out-dir", "--output", "-o", dest="out", default=None,
                    help="output file or directory path")
    db.add_argument("--watch", action="store_true",
                    help="regenerate the dashboard in a loop instead of once (Ctrl+C to stop)")
    db.add_argument("--interval", type=int, default=None,
                    help=f"seconds between regenerations in --watch mode (default: {_DEFAULT_WATCH_INTERVAL})")
    db.set_defaults(func=cmd_dashboard)

    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
