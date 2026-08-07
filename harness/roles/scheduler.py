#!/usr/bin/env python
"""Scheduler role (Stage 3, §9 step ③).

Assigns roles to tasks, creates one git worktree per task (§3.1 isolation),
and issues a lease (§6.3). Starts serial (one task at a time); parallel
assignment is Stage 3b.

All ledger events go through the Sequencer (H3: single writer).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path


def topo_order(tasks: list[dict]) -> list[str]:
    """Return task_ids in dependency order (dependencies first). Cyclic-safe."""
    ids = [t["task_id"] for t in tasks]
    deps = {t["task_id"]: list(t.get("depends_on", [])) for t in tasks}
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(u: str):
        if u in seen:
            return
        seen.add(u)
        for d in deps.get(u, []):
            if d in ids:
                visit(d)
        ordered.append(u)

    for tid in ids:
        visit(tid)
    return ordered


def create_worktree(task_id: str, root: str = "workspaces", git=None, dry_run: bool = False) -> dict:
    """Create `git worktree add <root>/<task_id> -b task/<task_id>`.

    `git` is injectable for testing; defaults to subprocess. Returns a dict
    with path/branch/ok/error. When dry_run, plans the command without running it.
    """
    path = str(Path(root, task_id).as_posix())
    branch = f"task/{task_id}"
    cmd = ["git", "worktree", "add", path, "-b", branch]
    if dry_run:
        return {"path": path, "branch": branch, "ok": True, "cmd": cmd, "dry_run": True}
    if git is None:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, shell=False)
            ok = proc.returncode == 0
            return {"path": path, "branch": branch, "ok": ok,
                    "cmd": cmd,
                    "error": None if ok else (proc.stderr or proc.stdout).strip()}
        except FileNotFoundError as e:
            return {"path": path, "branch": branch, "ok": False, "cmd": cmd, "error": str(e)}
    # injected (test): just return the planned command
    return {"path": path, "branch": branch, "ok": True, "cmd": cmd}


def schedule(task_id: str, tasks: list[dict], vendor: str = "claude",
             role: str = "implementer", lease_seconds: int = 3600,
             root: str = "workspaces", dry_run: bool = False,
             seq=None) -> dict:
    """Schedule the given task DAG: serial worktree creation + lease issuance.

    ledger events per task: worktree.created (or worktree.error) + task.leased.
    Returns {"ok": True, "order": [...]} or {"ok": False, "errors": [...]}.
    """
    order = topo_order(tasks)
    by_id = {t["task_id"]: t for t in tasks}
    errors: list[str] = []

    for tid in order:
        t = by_id.get(tid, {})
        if dry_run:
            if seq is not None:
                seq.propose(tid, "task.scheduled", dry_run=True,
                            worktree_cmd=create_worktree(tid, root, dry_run=True)["cmd"])
            continue
        wt = create_worktree(tid, root)
        if wt["ok"]:
            if seq is not None:
                seq.propose(tid, "worktree.created", path=wt["path"], branch=wt["branch"])
        else:
            errors.append(f"{tid}: worktree creation failed: {wt['error']}")
            if seq is not None:
                seq.propose(tid, "worktree.error", error=wt["error"])
            continue
        lease_until = time.time() + lease_seconds
        if seq is not None:
            seq.propose(tid, "task.leased", vendor=vendor, role=role,
                        lease_until=lease_until,
                        touch_allow=t.get("touch_allow", []))

    return {"ok": not errors, "order": order, "errors": errors}
