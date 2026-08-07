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

    Idempotent & robust to stale state:
      - if the branch is already checked out in another worktree, reuse it;
      - if the branch already exists (not checked out), add without `-b`;
      - otherwise create a fresh branch.
    """
    path = str(Path(root, task_id).as_posix())
    branch = f"task/{task_id}"
    cmd = ["git", "worktree", "add", path, "-b", branch]
    if dry_run:
        return {"path": path, "branch": branch, "ok": True, "cmd": cmd, "dry_run": True}

    def run(c):
        if git is not None:
            return git(c)
        return subprocess.run(c, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", shell=False)

    # 1) branch already checked out in an existing worktree -> reuse it
    lst = run(["git", "worktree", "list", "--porcelain"]).stdout or ""
    for line in lst.splitlines():
        # lines look like: "worktree <path>" then later "branch refs/heads/task/T1"
        pass
    # parse porcelain: groups separated by blank lines
    current_path = None
    for line in lst.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/") and current_path:
            existing = line[len("branch refs/heads/"):].strip()
            if existing == branch and Path(current_path).exists():
                return {"path": current_path, "branch": branch, "ok": True,
                        "reused": True, "cmd": cmd}

    if Path(path).exists():
        # path present but not a tracked worktree: reuse as-is
        return {"path": path, "branch": branch, "ok": True, "reused": True, "cmd": cmd}

    proc = run(cmd)
    if proc.returncode == 0:
        return {"path": path, "branch": branch, "ok": True, "cmd": cmd}
    # branch already exists -> add using the existing branch (no -b)
    if "already exists" in (proc.stderr or proc.stdout):
        cmd2 = ["git", "worktree", "add", path, branch]
        proc2 = run(cmd2)
        if proc2.returncode == 0:
            return {"path": path, "branch": branch, "ok": True, "cmd": cmd2}
        return {"path": path, "branch": branch, "ok": False,
                "error": (proc2.stderr or proc2.stdout).strip()}
    return {"path": path, "branch": branch, "ok": False,
            "error": (proc.stderr or proc.stdout).strip()}


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
