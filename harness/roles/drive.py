#!/usr/bin/env python
"""Drive role (Stage B): run the full one-task pipeline for every task in a DAG.

Orchestrates plan -> implement -> review -> integrate, reusing the existing
role functions. Stage B parallel (b) fans the `implement` role out into multiple
channels (multi-vendor / multi-model): each channel runs in its own worktree and
branch in parallel, and the first channel whose review passes is integrated.

All ledger events flow through the shared Sequencer (single writer, thread-safe).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from harness.core.ledger import Sequencer
from harness.roles.decomposer import (
    decompose as decomposer_decompose,
    parse_tasks_md,
    render_tasks_md,
    structural_check,
)
from harness.roles.scheduler import create_worktree, schedule, topo_order
from harness.roles.implementer import implement
from harness.roles.review_flow import run_pipeline
from harness.roles.integrator import integrate
from harness.core.verifiers import VerifierRegistry
from harness.core.invoke import resolve_role, resolve_role_channels

def _resolve_acceptance(task: dict, tasks_text: str | None = None) -> list[dict]:
    acc = task.get("acceptance") or []
    return [{"verb": a.get("verb", ""), "args": a.get("args", []),
             "expect_exit": a.get("expect_exit", 0)} for a in acc]


def _channel_worktree_id(task_id: str, vendor: str, idx: int) -> str:
    """Composite id so each (task, channel) owns an isolated worktree/branch."""
    return f"{task_id}__{vendor}_{idx}"


def drive(
    requirement: str,
    spec_path: str | None,
    tasks_path: str,
    target_branch: str = "main",
    seq: Sequencer | None = None,
    dry_run: bool = False,
    implement_vendor: str | None = None,
    reviewer_vendor: str | None = None,
    implement_channels: list[dict] | None = None,
) -> dict:
    """Drive every task in the DAG through implement -> review -> integrate.

    Stage B parallel (b): the `implement` role fans out into multiple channels
    (multi-vendor / multi-model). Each channel runs in its own worktree/branch in
    parallel (ThreadPoolExecutor); the first channel whose review passes is
    integrated, the rest are discarded.

    Channel declaration (precedence):
      1. `implement_channels` arg (parsed from CLI `--implement-vendors "agy:2,hermes:3"`)
      2. `roles.implement` list in vendors.yaml
         (e.g. `[{vendor:agy,...},{vendor:hermes,...},{vendor:hermes,...}]`)
      3. legacy single dict `roles.implement: {vendor, model, effort}` (1 channel)

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

    # Always ensure worktrees exist (idempotent). drive's --dry-run only skips
    # vendor calls and the integrate git step.
    schedule("T-drive" if seq is None else f"T-{uuid_short()}", tasks,
             root="workspaces", dry_run=False, seq=seq)

    order = topo_order(tasks)
    by_id = {t["task_id"]: t for t in tasks}
    results: list[dict] = []

    # Resolve the implement channel fan-out once (shared by all tasks).
    if implement_vendor:
        channels = [{"vendor": implement_vendor, "model": None, "effort": None}]
    else:
        channels = resolve_role_channels(
            "implement", config_dir, explicit_override=implement_channels
        )

    for tid in order:
        task = by_id.get(tid, {})
        entry: dict[str, Any] = {"task_id": tid}

        # ④ implement — fan out across channels, run in parallel
        ch_results: list[dict] = []
        default_vendor = resolve_role("implement", config_dir)["vendor"]
        single_path = (len(channels) == 1 and
                       channels[0]["vendor"] == (implement_vendor or default_vendor))
        if single_path:
            # Backward-compatible single-channel path: use the plain worktree/branch.
            ch = channels[0]
            wt = str(Path("workspaces").resolve() / tid)
            impl = implement(tid, task, wt, vendor=ch["vendor"],
                             model=ch["model"], effort=ch["effort"],
                             seq=seq, dry_run=dry_run)
            ch_results.append({"vendor": ch["vendor"], "model": ch["model"],
                               "effort": ch["effort"], "worktree": wt,
                               "task_id": tid, "impl": impl})
        else:
            # Multi-channel: one worktree/branch per (task, channel).
            def _run_channel(i: int, ch: dict) -> dict:
                cid = _channel_worktree_id(tid, ch["vendor"], i)
                wt = str(Path("workspaces").resolve() / cid)
                create_worktree(cid, root="workspaces", dry_run=dry_run)
                impl = implement(cid, task, wt, vendor=ch["vendor"],
                                 model=ch["model"], effort=ch["effort"],
                                 seq=seq, dry_run=dry_run)
                return {"vendor": ch["vendor"], "model": ch["model"],
                        "effort": ch["effort"], "worktree": wt,
                        "task_id": cid, "impl": impl}

            with ThreadPoolExecutor(max_workers=len(channels)) as ex:
                ch_results = list(ex.map(
                    lambda kv: _run_channel(kv[0], kv[1]),
                    list(enumerate(channels)),
                ))

        entry["implement"] = {
            "channels": [
                {"vendor": c["vendor"], "model": c["model"], "effort": c["effort"],
                 "ok": c["impl"].get("ok"), "commit": c["impl"].get("commit")}
                for c in ch_results
            ]
        }

        # ⑤⑥⑦ review — each channel, pick the first that passes
        acc = _resolve_acceptance(task)
        rev_role = resolve_role("review", config_dir)
        rev_vendor = reviewer_vendor or rev_role["vendor"]
        winner: dict | None = None
        reviews: list[dict] = []
        for c in ch_results:
            rev = run_pipeline(c["task_id"], c["worktree"], acc,
                               reviewer_vendor=rev_vendor,
                               dry_run=dry_run, seq=seq,
                               model=rev_role["model"], effort=rev_role["effort"])
            verdict = rev.get("verdict")
            reviews.append({"vendor": c["vendor"], "verdict": verdict})
            if winner is None and verdict in ("pass", "pass_with_findings"):
                winner = c
        entry["review"] = {
            "channels": reviews,
            "judgment_unavailable": any(
                r.get("verdict") is None for r in reviews
            ),
        }
        first_verdict = reviews[0]["verdict"] if reviews else None
        entry["review"]["verdict"] = first_verdict  # mirror single-channel shape

        # ⑧ integrate — only the winning channel (first that passed)
        if not dry_run and winner is not None:
            integ = integrate(winner["task_id"], task, winner["worktree"],
                             target_branch=target_branch, seq=seq, dry_run=dry_run)
            entry["integrate"] = {"ok": integ.get("ok"),
                                  "commit": integ.get("commit"),
                                  "winner": winner["vendor"]}
        else:
            entry["integrate"] = {"skipped": True,
                                  "reason": "dry_run" if dry_run else "no passing channel"}

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
