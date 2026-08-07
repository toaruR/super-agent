#!/usr/bin/env python
"""Implementer role (Stage 4, §9 step ④).

Runs the Implementer vendor inside a task's git worktree, constrained to the
task's `touch_allow` paths, then commits the result and records it on the
ledger as `artifact.produced` (+ `task.implemented`).

§3.1 isolation: the vendor only ever sees its own worktree; §6.2 `touch_allow`
is the allow-list it may modify. The harness (not the vendor) performs the
commit so the evidence (`tree_hash`/commit) is bound deterministically.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from harness.core.invoke import invoke, load_vendors
from harness.core.ledger import Sequencer

IMPLEMENT_PROMPT = """\
あなたは Implementer です。以下のタスクを実装してください。

# タスク
タスクID: {task_id}
目標: {goal}

# 受入基準（これらが通ること）
{acceptance}

# 触ってよい範囲（このリストのファイル以外は作成・変更してはいけない）
{touch_allow}

# 制約
- 上記 `touch_allow` に列挙されたファイル・パスだけを作成・変更すること。
- それ以外のファイル（設定、他タスクのファイル、README 等）には触らないこと。
- コミットは harness が行うので、あなたはコミットしなくてよい。
- 実装が終わったら、受入基準を自分で確認できる範囲で満たしているか考えよ。
"""


def _fmt_acceptance(task: dict) -> str:
    out = []
    for a in task.get("acceptance", []):
        verb = a.get("verb", "")
        args = " ".join(a.get("args", []))
        out.append(f"- `{verb} {args}` (expect_exit={a.get('expect_exit', 0)})")
    return "\n".join(out) if out else "- （なし）"


def implement(task_id: str, task: dict, worktree_path: str,
              vendor: str = "claude", seq: Sequencer | None = None,
              dry_run: bool = False) -> dict:
    """Implement a single task inside its worktree and commit.

    Returns a payload with ok/commit/cmd. Records ledger events when seq given.
    """
    goal = task.get("goal", "")
    touch_allow = task.get("touch_allow", [])
    prompt = IMPLEMENT_PROMPT.format(
        task_id=task_id,
        goal=goal,
        acceptance=_fmt_acceptance(task),
        touch_allow="\n".join(f"- {p}" for p in touch_allow) or "- （なし）",
    )

    decls = load_vendors(Path(__file__).resolve().parent.parent / "config")
    decl = decls.get(vendor, decls["claude"])
    cmd = decl.headless(prompt)

    if dry_run:
        return {"ok": True, "dry_run": True, "cmd": cmd, "task_id": task_id}

    # run the vendor inside the worktree
    try:
        proc = subprocess.run(
            cmd, cwd=str(worktree_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=False,
        )
    except FileNotFoundError as e:
        if seq is not None:
            seq.propose(task_id, "implementer.error", error=str(e))
        return {"ok": False, "task_id": task_id, "error": str(e)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if seq is not None:
            seq.propose(task_id, "implementer.error", error=err)
        return {"ok": False, "task_id": task_id, "error": err}

    # harness performs the commit (touch_allow allow-list)
    commit = _commit_worktree(task_id, worktree_path, touch_allow, seq)
    if not commit.get("ok"):
        return {"ok": False, "task_id": task_id,
                "error": commit.get("error"), "vendor_rc": proc.returncode}

    if seq is not None:
        seq.propose(task_id, "artifact.produced",
                    paths=touch_allow, commit=commit.get("commit"))
        seq.propose(task_id, "task.implemented",
                    commit=commit.get("commit"),
                    tree_hash=commit.get("tree_hash"))

    return {
        "ok": True,
        "task_id": task_id,
        "commit": commit.get("commit"),
        "tree_hash": commit.get("tree_hash"),
        "paths": touch_allow,
        "cmd": cmd,
    }


def _commit_worktree(task_id: str, worktree_path: str, touch_allow: list[str],
                      seq: Sequencer | None) -> dict:
    """Stage the allow-listed paths in the worktree and commit.

    Uses `git -C <worktree>` so we never leave the worktree. If nothing
    changed, returns ok with commit=None (idempotent).
    """
    paths = touch_allow or ["."]
    add = ["git", "-C", str(worktree_path), "add", "--", *paths]
    try:
        ap = subprocess.run(add, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", shell=False)
        if ap.returncode != 0:
            return {"ok": False, "error": (ap.stderr or ap.stdout).strip()}
        # detect staged changes
        diff = subprocess.run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        if diff.returncode == 0:
            # nothing staged -> nothing to commit
            return {"ok": True, "commit": None, "tree_hash": None}
        msg = f"{task_id}: implement\n\nallow-list: {', '.join(touch_allow) or '(all)'}"
        cp = subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", msg],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        if cp.returncode != 0:
            return {"ok": False, "error": (cp.stderr or cp.stdout).strip()}
        # capture commit hash + tree hash (evidence binding, §3.2)
        ch = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        th = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        return {"ok": True,
                "commit": ch.stdout.strip() or None,
                "tree_hash": th.stdout.strip() or None}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
