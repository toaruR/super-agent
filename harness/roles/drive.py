#!/usr/bin/env python
"""Drive role (Stage B): run the full one-task pipeline for every task in a DAG.

Orchestrates plan -> implement -> review -> integrate, reusing the existing
role functions. Stage B parallel (b) fans the `implement` role out into multiple
channels (multi-vendor / multi-model): each channel runs in its own worktree and
branch in parallel, and the first channel whose review passes is integrated.

Task-level parallelism (Stage B parallel, task fan-out): independent tasks (per
topo layers) run concurrently during the implement+review phase; the integrate
phase (git checkout/merge on the shared repo root) is always serialized to avoid
concurrent git mutations.

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
from harness.roles import planner as planner_role
from harness.roles.scheduler import (
    create_worktree,
    schedule,
    teardown_worktree,
    topo_layers,
    topo_order,
)
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
    parallel_tasks: bool = True,
    max_task_workers: int = 4,
    speculative: bool = False,
    adaptive: bool = True,
) -> dict:
    """Drive every task in the DAG through implement -> review -> integrate.

    Default mode (speculative=False): each task is implemented in a SINGLE
    channel (the first declared implement channel, or the one selected via
    --implement-vendor / --implement-vendors with a single entry). Independent
    tasks run concurrently by default (task-level parallelism) — this is the
    default way to go faster, not speculative multi-channel fan-out.

    Speculative mode (speculative=True, or when --implement-vendors lists
    multiple channels): the `implement` role fans out into multiple channels
    (multi-vendor / multi-model). Each channel runs in its own worktree/branch
    in parallel and the first channel whose review passes is integrated; the
    rest are discarded. This is an OPT-IN mode, not the default.

    Adaptive re-planning (adaptive=True, default): between topo layers, the
    `planner` role re-examines the DAG against what actually happened (ledger
    events). It may carve out INVESTIGATION tasks (run first, before real work
    fans out), merge over-split tasks that share a file (can't be parallel
    worktrees), or re-order so interface-defining tasks run before consumers.
    Set adaptive=False to stick to the static initial DAG.

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
                 dry_run=dry_run, create_worktrees=False, seq=seq)
        render_tasks_md_safe(tasks, requirement, tasks_file)
        reused = False

    errs = structural_check(tasks, registry)
    if errs:
        return {"ok": False, "error": "structural_check failed", "errors": errs}

    # Channel worktrees are created per-channel inside _run_task_pipeline
    # (and torn down after integrate), so schedule here only issues leases —
    # do NOT create a parent worktree that nothing would tear down.
    schedule("T-drive" if seq is None else f"T-{uuid_short()}", tasks,
             root="workspaces", dry_run=False, create_worktrees=False, seq=seq)

    # Ensure the shared repo root is on the target branch before any integrate
    # merges into it. integrate() merges task/<id> into the *current* branch of
    # the repo root, so the root must already be at target_branch (create it if
    # missing). We restore the prior branch afterwards to avoid surprising the
    # caller's working tree.
    import subprocess as _sp
    _prev_branch = _sp.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=".", capture_output=True, text=True).stdout.strip()
    _co = _sp.run(["git", "checkout", target_branch], cwd=".",
                  capture_output=True, text=True)
    if _co.returncode != 0:
        _cb = _sp.run(["git", "checkout", "-b", target_branch], cwd=".",
                      capture_output=True, text=True)
        if _cb.returncode != 0:
            return {"ok": False, "error": f"cannot checkout target_branch {target_branch}: "
                    f"{_co.stderr or _cb.stderr}"}

    order = topo_order(tasks)
    by_id = {t["task_id"]: t for t in tasks}
    results: list[dict] = []
    results_by_id: dict[str, dict] = {}

    # Resolve the implement channel fan-out once (shared by all tasks).
    # Default (speculative=False): collapse to a SINGLE channel so each task is
    # implemented once (no racing). Speculative mode keeps the full fan-out.
    if implement_vendor:
        channels = [{"vendor": implement_vendor, "model": None, "effort": None}]
    else:
        channels = resolve_role_channels(
            "implement", config_dir, explicit_override=implement_channels
        )
        if not speculative:
            # non-speculative default: implement each task in a single channel
            channels = channels[:1]

    def _run_task_pipeline(tid: str) -> dict:
        """Phase A: implement (channels parallel) + review, pick winner.
        No git mutation here, so it is safe to run concurrently across tasks."""
        task = by_id.get(tid, {})
        entry: dict[str, Any] = {"task_id": tid}
        channel_ids: list[str] = []

        try:
            # ④ implement — fan out across channels, run in parallel
            ch_results: list[dict] = []
            default_vendor = resolve_role("implement", config_dir)["vendor"]
            single_path = (len(channels) == 1 and
                           channels[0]["vendor"] == (implement_vendor or default_vendor))
            if single_path:
                # Backward-compatible single-channel path: use the plain worktree/branch.
                ch = channels[0]
                wt = str(Path("workspaces").resolve() / tid)
                cw = create_worktree(tid, root="workspaces", dry_run=dry_run)
                if not cw.get("ok"):
                    return {"vendor": ch["vendor"], "model": ch["model"],
                            "effort": ch["effort"], "task_id": tid,
                            "ok": False, "error": cw.get("error", "worktree create failed")}
                impl = implement(tid, task, wt, vendor=ch["vendor"],
                                 model=ch["model"], effort=ch["effort"],
                                 seq=seq, dry_run=dry_run)
                ch_results.append({"vendor": ch["vendor"], "model": ch["model"],
                                   "effort": ch["effort"], "worktree": wt,
                                   "task_id": tid, "impl": impl, "ok": True})
                channel_ids.append(tid)
            else:
                # Multi-channel: one worktree/branch per (task, channel).
                def _run_channel(i: int, ch: dict) -> dict:
                    cid = _channel_worktree_id(tid, ch["vendor"], i)
                    wt = str(Path("workspaces").resolve() / cid)
                    cw = create_worktree(cid, root="workspaces", dry_run=dry_run)
                    if not cw.get("ok"):
                        return {"vendor": ch["vendor"], "model": ch["model"],
                                "effort": ch["effort"], "task_id": cid,
                                "ok": False, "error": cw.get("error", "worktree create failed")}
                    impl = implement(cid, task, wt, vendor=ch["vendor"],
                                     model=ch["model"], effort=ch["effort"],
                                     seq=seq, dry_run=dry_run)
                    return {"vendor": ch["vendor"], "model": ch["model"],
                            "effort": ch["effort"], "worktree": wt,
                            "task_id": cid, "impl": impl, "ok": True}

                with ThreadPoolExecutor(max_workers=len(channels)) as ex:
                    ch_results = list(ex.map(
                        lambda kv: _run_channel(kv[0], kv[1]),
                        list(enumerate(channels)),
                    ))
                channel_ids = [_channel_worktree_id(tid, c["vendor"], i)
                               for i, c in enumerate(channels)]

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
                if not c.get("ok"):
                    # channel worktree failed to create/run — record and skip
                    reviews.append({"vendor": c.get("vendor"), "verdict": None,
                                    "error": c.get("error")})
                    continue
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
            entry["_winner"] = winner  # internal: used by the serial integrate phase
            entry["_channel_ids"] = channel_ids  # internal: teardown after integrate
            return entry
        except Exception as ex:
            # any failure in the pipeline must not abort the whole drive nor
            # leave channel worktrees behind — record and let Phase B tear down.
            entry["error"] = str(ex)
            entry["_winner"] = None
            entry["_channel_ids"] = channel_ids
            entry["review"] = {"channels": [], "judgment_unavailable": True,
                               "verdict": None, "error": str(ex)}
            return entry

    # Phase A: run task pipelines. Independent tasks (topo layers) run in parallel;
    # layers themselves run serially so dependencies are satisfied before a task
    # is implemented. Each task's channels are already parallel inside _run_task.
    if parallel_tasks:
        layers = topo_layers(tasks)
        for layer in layers:
            # --- Adaptive re-planning (planner) BEFORE this layer ---
            if adaptive and seq is not None:
                events = seq.load()
                rep = planner_role.replan(
                    requirement, tasks, events=events,
                    vendor=resolve_role("planner", config_dir)["vendor"],
                    existing_design=spec_path or "",
                    model=resolve_role("planner", config_dir).get("model"),
                    seq=seq, dry_run=dry_run,
                )
                if rep.get("ok") and rep.get("tasks"):
                    tasks = rep["tasks"]
                    by_id = {t["task_id"]: t for t in tasks}
                    order = topo_order(tasks)
                # Investigation tasks run FIRST (before this layer's real work).
                for it in rep.get("investigation_needed", []):
                    itid = it.get("task_id", "investigate")
                    if itid in by_id and itid not in layer:
                        results_by_id[itid] = _run_task_pipeline(itid)
            with ThreadPoolExecutor(max_workers=max_task_workers) as ex:
                for entry in ex.map(_run_task_pipeline, layer):
                    results_by_id[entry["task_id"]] = entry
    else:
        for tid in order:
            results_by_id[tid] = _run_task_pipeline(tid)

    # Phase B: integrate winners serially (git checkout/merge on the shared repo
    # root must not run concurrently). Ordered by topo_order so a child is merged
    # after its parents. Then tear down every channel worktree so loser channels
    # don't linger.
    for tid in order:
        entry = results_by_id.get(tid)
        if entry is None:
            continue
        task = by_id.get(tid, {})
        winner = entry.pop("_winner", None)
        channel_ids = entry.pop("_channel_ids", [tid])
        if not dry_run and winner is not None:
            try:
                integ = integrate(winner["task_id"], task, winner["worktree"],
                                 target_branch=target_branch, seq=seq, dry_run=dry_run)
                entry["integrate"] = {"ok": integ.get("ok"),
                                      "commit": integ.get("commit"),
                                      "winner": winner["vendor"]}
                if not integ.get("ok"):
                    entry["integrate"]["error"] = integ.get("error")
            except Exception as ex:
                # integrate must never abort the whole drive; record and move on
                entry["integrate"] = {"ok": False, "winner": winner["vendor"],
                                      "error": str(ex)}
                if seq is not None:
                    seq.propose(winner["task_id"], "integrate.error",
                                error=str(ex)[:300])
        else:
            entry["integrate"] = {"skipped": True,
                                  "reason": "dry_run" if dry_run else "no passing channel"}
        # cleanup: remove all channel worktrees/branches for this task
        if not dry_run:
            for cid in channel_ids:
                teardown_worktree(cid, root="workspaces", dry_run=dry_run)
        results.append(entry)

    # Restore the caller's branch so drive() doesn't leave the repo on the
    # target branch as a side effect.
    if not dry_run and _prev_branch:
        _sp.run(["git", "checkout", _prev_branch], cwd=".",
                capture_output=True, text=True)

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
