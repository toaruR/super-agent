#!/usr/bin/env python
"""Drive role (Stage B): run the full one-task pipeline for every task in a DAG.

Orchestrates plan -> implement -> review -> integrate, reusing the existing
role functions. Starts serial (one task at a time); parallel execution is a
later extension that swaps the loop for a thread/process pool over the same
per-task function.

All ledger events flow through the shared Sequencer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.core.ledger import Sequencer
from harness.roles.decomposer import (
    decompose as decomposer_decompose,
    parse_tasks_md,
    render_tasks_md,
    structural_check,
)
from harness.roles.scheduler import schedule, topo_order
from harness.roles.implementer import implement
from harness.roles.review_flow import run_pipeline
from harness.roles.integrator import integrate
from harness.core.verifiers import VerifierRegistry
from harness.core.invoke import resolve_role

def _resolve_acceptance(task: dict, tasks_text: str | None = None) -> list[dict]:
    acc = task.get("acceptance") or []
    return [{"verb": a.get("verb", ""), "args": a.get("args", []),
             "expect_exit": a.get("expect_exit", 0)} for a in acc]


def drive(
    requirement: str,
    spec_path: str | None,
    tasks_path: str,
    target_branch: str = "main",
    seq: Sequencer | None = None,
    dry_run: bool = False,
    implement_vendor: str | None = None,
    reviewer_vendor: str | None = None,
) -> dict:
    """Drive every task in the DAG through implement -> review -> integrate.

    If `tasks_path` does not exist, decompose from `requirement`+`spec_path` first
    (creating worktrees via schedule) and write the DAG to `tasks_path`.
    Returns a summary dict: {ok, tasks:[{task_id, implement, review, integrate}]}.
    """
    tasks_file = Path(tasks_path)
    config_dir = Path(__file__).resolve().parent.parent / "config"
    registry = VerifierRegistry(config_dir / "verifiers.yaml")

    if tasks_file.exists():
        tasks = parse_tasks_md(str(tasks_file))
        reused = True
    else:
        if seq is not None:
            tid = f"T-{uuid_short()}"
            seq.propose(tid, "task.created", goal=requirement, role="decomposer")
        out = decomposer_decompose(
            tid if seq is not None else "T-drive",
            requirement, vendor=resolve_role("design", config_dir)["vendor"], existing_design=spec_path or "",
            dry_run=dry_run, seq=seq,
        )
        if not out.get("ok"):
            return {"ok": False, "error": "decompose failed", "detail": out}
        tasks = out.get("tasks", [])
        schedule(tid if seq is not None else "T-drive", tasks, root="workspaces",
                 dry_run=dry_run, seq=seq)
        render_tasks_md_safe(tasks, requirement, tasks_file)
        reused = False

    errs = structural_check(tasks, registry)
    if errs:
        return {"ok": False, "error": "structural_check failed", "errors": errs}

    # Always ensure worktrees exist (idempotent: reuses the task/<id> branch and
    # recreates the directory if pruned). This makes CVE have a real dir to verify.
    # schedule's own dry_run is NOT passed — we always materialize worktrees; drive's
    # --dry-run only skips vendor calls and the integrate git step.
    schedule("T-drive" if seq is None else f"T-{uuid_short()}", tasks,
             root="workspaces", dry_run=False, seq=seq)

    order = topo_order(tasks)
    by_id = {t["task_id"]: t for t in tasks}
    results: list[dict] = []

    for tid in order:
        task = by_id.get(tid, {})
        worktree = str(Path("workspaces") / tid)
        entry: dict[str, Any] = {"task_id": tid}

        # ④ implement
        impl_vendor = implement_vendor or resolve_role("implement", config_dir)["vendor"]
        impl = implement(tid, task, worktree, vendor=impl_vendor,
                         seq=seq, dry_run=dry_run)
        entry["implement"] = {"ok": impl.get("ok"), "commit": impl.get("commit")}

        # ⑤⑥⑦ review
        acc = _resolve_acceptance(task)
        rev = run_pipeline(tid, worktree, acc,
                           reviewer_vendor=reviewer_vendor or resolve_role("review", config_dir)["vendor"],
                           dry_run=dry_run, seq=seq)
        entry["review"] = {"verdict": rev.get("verdict"),
                            "judgment_unavailable": rev.get("judgment") == "judgment_unavailable"}

        # ⑧ integrate (only if review passed and not dry_run)
        if not dry_run and rev.get("verdict") in ("pass", "pass_with_findings"):
            integ = integrate(tid, task, worktree, target_branch=target_branch,
                             seq=seq, dry_run=dry_run)
            entry["integrate"] = {"ok": integ.get("ok"), "commit": integ.get("commit")}
        else:
            entry["integrate"] = {"skipped": True,
                                  "reason": "dry_run" if dry_run else "review not passed"}

        results.append(entry)

    return {"ok": True, "reused_tasks_file": reused, "tasks": results}


def uuid_short() -> str:
    import uuid
    return f"T-{uuid.uuid4().hex[:8]}"


def render_tasks_md_safe(tasks: list[dict], requirement: str, path: Path) -> None:
    try:
        from harness.roles.decomposer import render_tasks_md
        path.write_text(render_tasks_md(tasks, requirement), encoding="utf-8")
    except Exception:
        pass
