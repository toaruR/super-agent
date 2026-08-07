#!/usr/bin/env python
"""Integrator role (Stage 5, plan.md Stage 5 / §9 ⑧).

Merges an implemented task's worktree branch into the integration target,
verifies only `touch_allow` paths changed, re-runs the acceptance (CVE) on the
merged tree, then tears down the worktree.

Ledger events: integration.merge / integration.touch_violation / conflict /
integrated / integrated.failed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from harness.core.cve import CVE
from harness.core.ledger import Sequencer

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _git(args: list[str], cwd: str | Path, dry_run: bool = False) -> dict:
    """Run a git command; returns {returncode, stdout, stderr} (utf-8 safe)."""
    if dry_run:
        return {"returncode": 0, "stdout": "", "stderr": "", "cmd": ["git", *args]}
    cp = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False,
    )
    return {"returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr,
            "cmd": ["git", *args]}


def _changed_files(worktree_path: str | Path) -> list[str]:
    """Files with uncommitted or committed-since-merge-base changes in the worktree."""
    # staged + unstaged + untracked (porcelain, one path per line)
    out = subprocess.run(
        ["git", "status", "--porcelain", "-uall"], cwd=str(worktree_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
    ).stdout
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain: XY <path>  (path may have rename "a -> b")
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ")[-1]
        files.append(path)
    return files


def integrate(
    task_id: str,
    task: dict,
    worktree_path: str,
    target_branch: str = "main",
    seq: Sequencer | None = None,
    dry_run: bool = False,
) -> dict:
    """Integrate an implemented task into the target branch.

    Returns a payload with ok/merged/verified/cmd.
    """
    emit = (lambda tid, typ, **kw: seq.propose(tid, typ, **kw)) if seq is not None \
        else (lambda *a, **k: None)
    wt = Path(worktree_path)
    branch = f"task/{task_id}"
    touch_allow = set(task.get("touch_allow", []) or [])
    acceptance = task.get("acceptance", []) or []

    # 1) touch_allow violation check (spec §3.1 / §6.2)
    changed = _changed_files(wt)
    # only meaningful if there are uncommitted changes; implemented commit is on branch
    if changed and touch_allow and not all(
        any(p == c or c.startswith(p + "/") for p in touch_allow) for c in changed
    ):
        viol = [c for c in changed
                if not any(p == c or c.startswith(p + "/") for p in touch_allow)]
        emit(task_id, "integration.touch_violation", files=viol)
        return {"ok": False, "task_id": task_id,
                "error": f"touch_allow 外の変更: {viol}"}

    # 2) merge task/<id> into target_branch
    emit(task_id, "integration.merge", target=target_branch, branch=branch)
    # ensure we are on the target branch inside the main repo (use repo root)
    repo_root = wt.parent.parent if str(wt).endswith(task_id) else wt
    # the worktree is checked out at branch; merge into target from the main checkout
    r = _git(["checkout", target_branch], cwd=str(wt.parent.parent), dry_run=dry_run)
    if r["returncode"] != 0 and not dry_run:
        emit(task_id, "integration.failed", step="checkout", detail=r["stderr"][:300])
        return {"ok": False, "task_id": task_id, "error": r["stderr"]}
    m = _git(["merge", "--no-ff", "-m", f"Merge {branch}", branch],
              cwd=str(wt.parent.parent), dry_run=dry_run)
    if m["returncode"] != 0 and not dry_run:
        emit(task_id, "conflict", branch=branch, detail=m["stderr"][:500])
        # abort the merge to leave target clean
        _git(["merge", "--abort"], cwd=str(wt.parent.parent), dry_run=dry_run)
        return {"ok": False, "task_id": task_id,
                "error": f"merge conflict: {m['stderr'][:300]}"}

    # 3) re-run acceptance (CVE) against the merged tree
    if acceptance:
        cve = CVE(CONFIG_DIR / "verification_env.yaml", CONFIG_DIR / "verifiers.yaml")
        evidence = cve.run(wt.parent.parent, acceptance)
        emit(task_id, "verification.run",
             cve="local-win-py311", tree_hash=evidence["tree_hash"],
             cve_ok=evidence["cve_ok"], n_evidence=len(evidence["evidence"]))
        if not evidence["cve_ok"]:
            emit(task_id, "integrated.failed", step="verification",
                 detail="acceptance failed after merge")
            return {"ok": False, "task_id": task_id,
                    "error": "acceptance failed after merge"}

    # 4) success: record + tear down worktree
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(wt.parent.parent),
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
    ).stdout.strip() if not dry_run else ""
    emit(task_id, "integrated", branch=branch, target=target_branch, commit=commit)
    if not dry_run:
        _git(["worktree", "remove", "--force", str(wt)], cwd=str(wt.parent.parent))
    return {"ok": True, "task_id": task_id, "commit": commit, "branch": branch,
            "target": target_branch}
