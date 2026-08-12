#!/usr/bin/env python
"""Integrator role (Stage 5, plan.md Stage 5 / §9 ⑧).

Merges an implemented task's worktree branch into the integration target,
verifies only `touch_allow` paths changed, re-runs the acceptance (CVE) on the
merged tree, then tears down the worktree.

Ledger events: integration.merge / integration.touch_violation /
integration.touch_allow_extended / conflict / integrated / integrated.failed.

Dynamic touch_allow extension (アプローチ3, see CLAUDE.md memory
project_touch_allow_dynamic_extension): decompose-time touch_allow can't
always predict every file an implementer needs to create (e.g. splitting a
module into a new helper file). Rather than rejecting the whole task, a
touch_allow violation whose paths are BOTH (a) newly created (not a
modification of a pre-existing file) and (b) not overlapping any other task's
declared touch_allow is auto-approved and merged, with an
`integration.touch_allow_extended` event recorded for audit. Modifications to
pre-existing files outside touch_allow are never auto-approved (a pre-existing
file may be relied on by another task, so "it's new" is the only safety
argument available and it doesn't apply).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from harness.core.cve import CVE
from harness.core.ledger import Sequencer
from harness.roles.decomposer import touch_overlaps
from harness.roles.scheduler import effective_worktree_id

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _git(args: list[str], cwd: str | Path, dry_run: bool = False) -> dict:
    """Run a git command; returns {returncode, stdout, stderr} (utf-8 safe)."""
    from harness.core.invoke import git_executable
    git_bin = git_executable()
    if dry_run:
        return {"returncode": 0, "stdout": "", "stderr": "", "cmd": [git_bin, *args]}
    try:
        cp = subprocess.run(
            [git_bin, *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=False,
        )
        return {"returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr,
                "cmd": [git_bin, *args]}
    except FileNotFoundError:
        return {"returncode": 1, "stdout": "", "stderr": "git executable not found in PATH",
                "cmd": [git_bin, *args]}


def _changed_files(worktree_path: str | Path) -> list[str]:
    """Files with uncommitted or committed-since-merge-base changes in the worktree."""
    from harness.core.invoke import git_executable
    git_bin = git_executable()
    # defensive: if the worktree dir is gone (already torn down), nothing to check
    if not Path(worktree_path).is_dir():
        return []
    # staged + unstaged + untracked (porcelain, one path per line)
    try:
        out = subprocess.run(
            [git_bin, "status", "--porcelain", "-uall"], cwd=str(worktree_path),
            capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
        ).stdout or ""
    except FileNotFoundError:
        return []
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


# Build/test artifact patterns that must never be auto-extended into
# touch_allow, even if a .gitignore gap lets one slip through as `??`
# (defense-in-depth on top of .gitignore, since this path is an unattended
# approval rather than an explicit human-declared touch_allow entry).
_DENYLISTED_SUFFIXES = (".pyc", ".tmp", ".log")
_DENYLISTED_NAMES = (".coverage", "coverage.xml")
_DENYLISTED_DIR_SEGMENTS = ("__pycache__", ".pytest_cache", "htmlcov")


def _is_denylisted_artifact(path: str) -> bool:
    """True if `path` looks like a build/test artifact rather than source
    the implementer intentionally created (see _DENYLISTED_* above)."""
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if name in _DENYLISTED_NAMES:
        return True
    if any(name.endswith(suf) for suf in _DENYLISTED_SUFFIXES):
        return True
    segments = p.split("/")
    return any(seg in _DENYLISTED_DIR_SEGMENTS for seg in segments)


def _new_paths(worktree_path: str | Path) -> set[str]:
    """Paths in the worktree that are newly created rather than a modification
    of a path that already existed in the branch's parent commit.

    Used to gate touch_allow auto-extension to new-file creation only: `??`
    (untracked) and any staged `A` (added) status mean the path did not exist
    before this task touched it, so extending the allow-list for it doesn't
    let the task retroactively rewrite something another task may depend on.
    Build/test artifacts (_is_denylisted_artifact) are excluded even here so
    a .gitignore gap can't turn into an auto-approved touch_allow entry.
    """
    if not Path(worktree_path).is_dir():
        return set()
    out = subprocess.run(
        ["git", "status", "--porcelain", "-uall"], cwd=str(worktree_path),
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
    ).stdout
    new: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ")[-1]
        if (status == "??" or status.startswith("A")) and not _is_denylisted_artifact(path):
            new.add(path)
    return new


def _commit_extension(worktree_path: str | Path, task_id: str, paths: list[str]) -> dict:
    """Stage and commit the dynamically-extended new-file paths onto the
    task's branch, inside its worktree, mirroring implementer._commit_worktree.

    The implementer only ever `git add`s its original touch_allow paths, so
    an extension candidate (outside that list) is still uncommitted in the
    worktree at this point; without this commit the branch merge in step 2
    would silently drop it (merge operates on commits, not working-tree
    state).
    """
    add = subprocess.run(
        ["git", "-C", str(worktree_path), "add", "--", *paths],
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
    )
    if add.returncode != 0:
        return {"ok": False, "error": (add.stderr or add.stdout).strip()}
    msg = f"{task_id}: touch_allow extension\n\nfiles: {', '.join(paths)}"
    cp = subprocess.run(
        ["git", "-C", str(worktree_path), "commit", "-m", msg],
        capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False,
    )
    if cp.returncode != 0:
        return {"ok": False, "error": (cp.stderr or cp.stdout).strip()}
    return {"ok": True}


def _extension_safe(path: str, task_id: str, all_tasks: list[dict] | None) -> bool:
    """A new-file path is safe to auto-extend touch_allow for iff it does not
    overlap any OTHER task's declared touch_allow (decomposer.touch_overlaps'
    prefix-aware rule, so a directory-scoped touch_allow on another task also
    blocks extension). `all_tasks is None` (caller didn't supply the DAG) is
    treated conservatively: we cannot verify safety, so extension is refused.
    """
    if all_tasks is None:
        return False
    for t in all_tasks:
        if t.get("task_id") == task_id:
            continue
        for other in t.get("touch_allow", []) or []:
            if touch_overlaps(path, other):
                return False
    return True


def integrate(
    task_id: str,
    task: dict,
    worktree_path: str,
    target_branch: str = "main",
    seq: Sequencer | None = None,
    dry_run: bool = False,
    design_file: str = "",
    all_tasks: list[dict] | None = None,
) -> dict:
    """Integrate an implemented task into the target branch.

    `all_tasks`, if given, is the full task DAG (as decomposed/replanned) and
    is used only to check dynamic touch_allow extension candidates (new files)
    against every OTHER task's declared touch_allow for conflicts (see
    _extension_safe). Without it, extension is never attempted and behavior is
    unchanged from before アプローチ3 (any out-of-allow-list change rejects).

    Returns a payload with ok/merged/verified/cmd.
    """
    emit = (lambda tid, typ, **kw: seq.propose(tid, typ, design_file=design_file, **kw)) if seq is not None \
        else (lambda *a, **k: None)
    wt = Path(worktree_path)
    eff_id = effective_worktree_id(task_id, design_file)
    branch = f"task/{eff_id}"
    touch_allow = set(task.get("touch_allow", []) or [])
    acceptance = task.get("acceptance", []) or []

    # 1) touch_allow violation check (spec §3.1 / §6.2), with dynamic
    #    extension for new files that don't conflict with other tasks
    #    (アプローチ3, see module docstring)
    changed = _changed_files(wt)
    # only meaningful if there are uncommitted changes; implemented commit is on branch
    if changed and touch_allow:
        viol = [c for c in changed
                if not any(p == c or c.startswith(p + "/") for p in touch_allow)]
        if viol:
            new_paths = _new_paths(wt)
            extendable = [v for v in viol if v in new_paths
                          and _extension_safe(v, task_id, all_tasks)]
            hard = [v for v in viol if v not in extendable]
            if extendable:
                if not dry_run:
                    commit_ext = _commit_extension(wt, task_id, extendable)
                    if not commit_ext.get("ok"):
                        emit(task_id, "integration.touch_violation", files=extendable,
                             detail=f"extension commit failed: {commit_ext.get('error')}")
                        return {"ok": False, "task_id": task_id,
                                "error": f"touch_allow 拡張のコミットに失敗: {commit_ext.get('error')}"}
                touch_allow = touch_allow | set(extendable)
                emit(task_id, "integration.touch_allow_extended",
                     files=sorted(extendable))
            if hard:
                emit(task_id, "integration.touch_violation", files=hard)
                return {"ok": False, "task_id": task_id,
                        "error": f"touch_allow 外の変更: {hard}"}

    # 2) merge task/<id> into target_branch
    emit(task_id, "integration.merge", target=target_branch, branch=branch)
    # ensure we are on the target branch inside the main repo (use repo root)
    repo_root = wt.parent.parent if str(wt).endswith(eff_id) else wt
    # the worktree is checked out at branch; merge into target from the main checkout
    # auto-create the target branch if it does not exist yet (e.g. a fresh
    # feature branch): try to check out the existing branch first; if that
    # fails (branch missing), create it with -b.
    r = _git(["checkout", target_branch], cwd=str(repo_root), dry_run=dry_run)
    if r["returncode"] != 0 and not dry_run:
        r = _git(["checkout", "-b", target_branch], cwd=str(repo_root), dry_run=dry_run)
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
