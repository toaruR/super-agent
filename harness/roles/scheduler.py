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


def topo_layers(tasks: list[dict]) -> list[list[str]]:
    """Partition task_ids into dependency layers for parallel execution.

    Layer 0 = tasks with no (in-graph) dependencies. Each subsequent layer holds
    tasks whose dependencies are all in earlier layers. Tasks within a layer have
    no inter-dependencies and can run concurrently. Cyclic-safe (a cycle collapses
    into whichever layer its members first reach).
    """
    ids = [t["task_id"] for t in tasks]
    deps = {t["task_id"]: [d for d in t.get("depends_on", []) if d in ids]
            for t in tasks}
    layer_of: dict[str, int] = {}
    changed = True
    # iterate to a fixed point: a node's layer = 1 + max(layer of deps), init 0
    for tid in ids:
        layer_of[tid] = 0
    while changed:
        changed = False
        for tid in ids:
            if deps[tid]:
                want = 1 + max(layer_of[d] for d in deps[tid])
                if want > layer_of[tid]:
                    layer_of[tid] = want
                    changed = True
    layers: list[list[str]] = []
    for tid in ids:
        while len(layers) <= layer_of[tid]:
            layers.append([])
        layers[layer_of[tid]].append(tid)
    return [layer for layer in layers if layer]


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

    if git is None:
        # clean stale/prunable worktree metadata so branches are not left
        # "checked out" at a deleted path (Windows + repeated runs)
        subprocess.run(["git", "worktree", "prune"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", shell=False)

    # 1) branch already checked out in an existing (live) worktree -> reuse it
    lst = run(["git", "worktree", "list", "--porcelain"]).stdout or ""
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
        # path present but not a tracked live worktree: reuse as-is
        return {"path": path, "branch": branch, "ok": True, "reused": True, "cmd": cmd}

    proc = run(cmd)
    if proc.returncode == 0:
        return {"path": path, "branch": branch, "ok": True, "cmd": cmd}
    # branch already exists / already checked out -> prune then retry once
    if "already exists" in (proc.stderr or proc.stdout) or "already checked out" in (proc.stderr or proc.stdout):
        if git is None:
            subprocess.run(["git", "worktree", "prune"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", shell=False)
        proc2 = run(["git", "worktree", "add", path, branch])
        if proc2.returncode == 0:
            return {"path": path, "branch": branch, "ok": True, "cmd": ["git", "worktree", "add", path, branch]}
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
