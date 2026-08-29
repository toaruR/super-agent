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
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from harness.core.ledger import Sequencer
from harness.roles.decomposer import (
    decompose as decomposer_decompose,
    parse_tasks_md,
    render_tasks_md,
    structural_check,
)
from harness.roles import planner as planner_role
from harness.roles.scheduler import (
    stable_tag,
    create_worktree,
    design_branch_name,
    schedule,
    teardown_worktree,
    topo_layers,
    topo_order,
)
from harness.roles.implementer import implement
from harness.roles.review_flow import run_pipeline
from harness.roles.integrator import integrate
from harness.core.verifiers import VerifierRegistry
from harness.core.invoke import resolve_role, resolve_role_channels, slugify

def _resolve_acceptance(task: dict, tasks_text: str | None = None) -> list[dict]:
    acc = task.get("acceptance") or []
    return [{"verb": a.get("verb", ""), "args": a.get("args", []),
             "expect_exit": a.get("expect_exit", 0)} for a in acc]


def _channel_worktree_id(task_id: str, vendor: str, idx: int) -> str:
    """Composite id so each (task, channel) owns an isolated worktree/branch.

    design_file disambiguation is handled separately by create_worktree()'s
    own `design_file` param (CRC32 tag), not baked in here.
    """
    return f"{task_id}__{vendor}_{idx}"


def drive(
    requirement: str,
    spec_path: str | None,
    tasks_path: str,
    target_branch: str | None = None,
    seq: Sequencer | None = None,
    dry_run: bool = False,
    implement_vendor: str | None = None,
    reviewer_vendor: str | None = None,
    implement_channels: list[dict] | None = None,
    parallel_tasks: bool = True,
    max_task_workers: int = 4,
    speculative: bool = False,
    adaptive: bool = True,
    implement_model: str | None = None,
    implement_effort: str | None = None,
    implement_timeout: int | None = None,
    task_file: str | None = None,
    resume: bool = True,
    push: bool = False,
    on_status_change: Callable[[], None] | None = None,
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

    Resume (resume=True, default): before running a task, drive checks the
    ledger for a prior `integrated` event for that (design_file, task_file,
    task_id) whose commit is still an ancestor of target_branch, and skips
    re-running it (implement/review/integrate) if found. This makes re-running
    `drive` on the same --task_file after a partial failure safe: only tasks
    that never integrated (or whose integration was since rolled back) are
    (re)executed. Only applies in non-speculative mode and when `seq` is
    given (the ledger is the source of truth); pass resume=False to force a
    full re-run of every task regardless of prior ledger state.

    Design/task commit (always, once target_branch is checked out): drive
    commits spec_path and its task-DAG directory (tasks_dir_for_design) onto
    target_branch before any task runs, so the branch always carries its own
    specs (mirrors the "docs: add X design and task DAG" commits users were
    making by hand). No-op if nothing changed (re-running drive on an
    already-committed design/task pair never fails on "nothing to commit").

    Push (push=False, default): when True, target_branch is pushed to
    `origin` once, after every task has finished (successfully or not),
    right before drive restores the caller's original branch.

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

    # Default merge target is derived from the design file (design/<stem>-<crc32>)
    # rather than a hardcoded "main", which git would otherwise auto-create even
    # when it was never the intended integration branch. Callers that pass
    # target_branch explicitly (e.g. `--target main`) still take precedence.
    if target_branch is None:
        target_branch = design_branch_name(spec_path or "")

    # Vendor prompts (decompose / replan) want the design's TEXT, not its path
    # string (a known-fixed bug: existing_design used to be handed spec_path
    # itself). Read once here; "" if spec_path is unset or doesn't exist yet
    # (e.g. drive auto-named a fresh design_file that hasn't been written).
    spec_text = ""
    if spec_path and Path(spec_path).exists():
        spec_text = Path(spec_path).read_text(encoding="utf-8", errors="ignore")

    if not requirement and spec_text:
        from harness.cli import extract_requirement_from_text
        requirement = extract_requirement_from_text(spec_text, default=Path(spec_path).stem)

    # Register the task root (design_file + task_file) up front so that every
    # downstream role can recover task_file from the ledger via resolve_task_file.
    # Named/status like cmd_plan's own decompose task (plan-<slug>-<hex>,
    # status="planning") so drive's plan phase is visible on the dashboard
    # too, not just a generic "T-<hex>"/"created" row. The hex tag is derived
    # (CRC32 of design_file+task_file, not uuid4) so re-running drive on the
    # same design/task_file lands on the SAME dashboard row instead of
    # minting a new "plan-...-<random>" entry every single run.
    effective_task_file = task_file or str(tasks_file.resolve())
    if seq is not None:
        tag = stable_tag((spec_path or "") + "|" + effective_task_file)
        tid = f"plan-{slugify(requirement or 'drive')}-{tag}"
        seq.propose(tid, "task.created", goal=requirement, role="decomposer",
                    design_file=spec_path or "",
                    task_file=effective_task_file,
                    status="planning")
        if on_status_change is not None:
            on_status_change()

    if tasks_file.exists():
        tasks = parse_tasks_md(str(tasks_file))
        reused = True
        if seq is not None:
            seq.propose(tid, "decompose.ok", n_tasks=len(tasks), source="tasks.md",
                        status="planned", design_file=spec_path or "",
                        task_file=effective_task_file)
            if on_status_change is not None:
                on_status_change()
    else:
        design_role = resolve_role("design", config_dir)
        out = decomposer_decompose(
            tid if seq is not None else "T-drive",
            requirement, vendor=design_role["vendor"], existing_design=spec_text,
            dry_run=dry_run, model=design_role["model"], effort=design_role["effort"],
            seq=seq, design_file=spec_path or "", timeout=design_role["timeout"],
        )
        if not out.get("ok"):
            if on_status_change is not None:
                on_status_change()
            return {"ok": False, "error": "decompose failed", "detail": out}
        tasks = out.get("tasks", [])
        schedule(tid if seq is not None else "T-drive", tasks, root="workspaces",
                 dry_run=dry_run, create_worktrees=False, seq=seq,
                 design_file=spec_path or "")
        render_tasks_md_safe(tasks, requirement, tasks_file)
        reused = False
        if on_status_change is not None:
            on_status_change()

    errs = structural_check(tasks, registry)
    if errs:
        return {"ok": False, "error": "structural_check failed", "errors": errs}

    # Channel worktrees are created per-channel inside _run_task_pipeline
    # (and torn down after integrate), so schedule here only issues leases —
    # do NOT create a parent worktree that nothing would tear down.
    schedule("T-drive" if seq is None else f"T-{uuid_short()}", tasks,
             root="workspaces", dry_run=False, create_worktrees=False, seq=seq,
             design_file=spec_path or "")

    # Ensure the shared repo root is on the target branch before any integrate
    # merges into it. integrate() merges task/<id> into the *current* branch of
    # the repo root, so the root must already be at target_branch (create it if
    # missing). We restore the prior branch afterwards to avoid surprising the
    # caller's working tree.
    #
    # Under test (SUPER_AGENT_TEST=1) we skip the real checkout/stash entirely so
    # a test invocation can never mutate the caller's working tree / branch.
    import os as _os
    import subprocess as _sp
    from harness.core.invoke import git_executable

    def _git_run(args: list[str], cwd: str = ".") -> _sp.CompletedProcess:
        cmd = ["git", *args]
        try:
            return _sp.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", shell=False)
        except FileNotFoundError:
            git_bin = git_executable()
            try:
                return _sp.run([git_bin, *args], cwd=cwd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", shell=False)
            except FileNotFoundError:
                return _sp.CompletedProcess(
                    args=[git_bin, *args], returncode=1, stdout="",
                    stderr="git command not found in PATH"
                )

    _head = _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=".").stdout.strip()
    _prev_branch = _head
    _stashed = False

    def _restore_branch() -> None:
        nonlocal _stashed
        if not dry_run and _prev_branch and _head != target_branch:
            _git_run(["checkout", _prev_branch], cwd=".")
            if _stashed:
                _pop = _git_run(["stash", "pop"], cwd=".")
                if _pop.returncode != 0:
                    # Don't silently drop the stash entry: whatever caused
                    # the pop to fail (lock contention, conflict) means the
                    # caller's pre-drive local edits are still sitting in
                    # `git stash list` instead of back on disk. Surfacing
                    # this beats losing uncommitted work without a trace.
                    print(f"[warn] drive-auto-stash pop failed, left in stash list: "
                          f"{(_pop.stderr or _pop.stdout).strip()}")
                else:
                    _stashed = False

    # Remember which spec_path/task_file (+ task-DAG dir) paths exist BEFORE
    # any branch switch: once _commit_design_and_tasks() below commits them,
    # they become tracked ONLY on target_branch, so _restore_branch()'s
    # checkout back to the caller's branch (which never had them) deletes
    # them from the working tree per normal git checkout semantics.
    # Re-materializing them afterwards (see _restore_docs_snapshot) keeps
    # drive() from deleting design/task files the caller had on disk before
    # calling it. Restored via `git checkout <target> -- <path>` (not a raw
    # byte copy) so autocrlf/other worktree filters produce the exact same
    # bytes git itself would check out, then unstaged so the path goes back
    # to being untracked, matching its state before drive() ran.
    _docs_paths_before: set[str] = set()

    def _snapshot_docs() -> None:
        from harness.core.invoke import tasks_dir_for_design
        for c in (spec_path, task_file or str(tasks_file)):
            if c and Path(c).is_file():
                _docs_paths_before.add(c)
        if spec_path:
            tdir = tasks_dir_for_design(spec_path)
            if tdir.is_dir():
                for f in tdir.rglob("*"):
                    if f.is_file():
                        _docs_paths_before.add(str(f))

    def _rel_to_cwd(p: str) -> str:
        try:
            return Path(p).resolve().relative_to(Path(".").resolve()).as_posix()
        except ValueError:
            return Path(p).as_posix()

    def _restore_docs_snapshot() -> None:
        for c in _docs_paths_before:
            if Path(c).exists():
                continue
            rel = _rel_to_cwd(c)
            _git_run(["checkout", target_branch, "--", rel], cwd=".")
            _git_run(["reset", "--", rel], cwd=".")

    def _stash_docs_if_untracked(cwd: str = ".") -> bool:
        """Stash (with -u, scoped to just the design/task doc paths) any of
        them that are currently untracked at `cwd`. A design/task doc file
        left over (untracked) from a prior drive() run on this same
        target_branch makes a full-branch `git checkout` refuse outright
        ("would be overwritten"), even when its content is byte-identical to
        what target_branch already tracks there (observed on Windows git).
        Scoped to just these paths (not a blanket -u stash) for the same
        reason the surrounding auto-stash skips -u: other untracked files
        (.gitignore, unrelated drafts) must never be touched."""
        rels = [_rel_to_cwd(p) for p in _docs_paths_before if Path(p).exists()]
        if not rels:
            return False
        # -c core.quotepath=false: Windows git otherwise quotes non-ASCII
        # paths (e.g. Japanese filenames) as C-style octal escapes in
        # --porcelain output, which line[3:].strip() below would pass through
        # unmodified as a bogus pathspec to `git stash push`, silently
        # stashing nothing and leaving the doc files untracked for the
        # subsequent full-branch checkout to reject.
        st = _git_run(["-c", "core.quotepath=false", "status", "--porcelain", "--", *rels], cwd=cwd)
        untracked = [line[3:].strip() for line in st.stdout.splitlines() if line.startswith("??")]
        if not untracked:
            return False
        s = _git_run(["stash", "push", "-u", "-m", "drive-docs-stash", "--", *untracked], cwd=cwd)
        return s.returncode == 0 and "No local changes" not in (s.stdout or "")

    def _pop_docs_stash_if_needed(stashed: bool, cwd: str = ".") -> None:
        if stashed:
            _git_run(["stash", "pop"], cwd=cwd)

    _snapshot_docs()

    if not _os.environ.get("SUPER_AGENT_TEST"):
        try:
            # If the repo root is ALREADY on the target branch, nothing to do.
            if _head != target_branch:
                # Do NOT use -u (--include-untracked) so untracked files (.gitignore, design files) are never stashed/removed.
                _st = _git_run(["stash", "push", "-m", "drive-auto-stash"], cwd=".")
                if _st.returncode == 0 and "No local changes" not in _st.stdout:
                    _stashed = True
                _docs_stashed = _stash_docs_if_untracked()
                _co = _git_run(["checkout", target_branch], cwd=".")
                if _co.returncode != 0:
                    _cb = _git_run(["checkout", "-b", target_branch], cwd=".")
                    _checkout_ok = _cb.returncode == 0
                else:
                    _checkout_ok = True
                _pop_docs_stash_if_needed(_docs_stashed)
                if not _checkout_ok:
                    _restore_branch()
                    return {"ok": False,
                            "error": f"cannot checkout target_branch {target_branch}: "
                                     f"{_co.stderr or _cb.stderr}"}
        except Exception as _ex:  # pragma: no cover - defensive
            _restore_branch()
            return {"ok": False, "error": f"checkout target_branch failed: {_ex}"}

    def _commit_design_and_tasks() -> None:
        """Commit spec_path + its task-DAG dir onto target_branch, once.

        Best-effort: `git add` whatever of {spec_path, tasks_dir_for_design(
        spec_path), task_file} exists on disk, then skip the commit entirely
        if nothing ended up staged — re-running drive() on a design/task pair
        that's already committed must not fail on "nothing to commit".
        """
        from harness.core.invoke import tasks_dir_for_design
        paths: list[str] = []
        if spec_path and Path(spec_path).exists():
            paths.append(Path(spec_path).as_posix())
            tdir = tasks_dir_for_design(spec_path)
            if tdir.is_dir():
                paths.append(tdir.as_posix())
        tpath = task_file or str(tasks_file)
        if tpath and Path(tpath).exists():
            tpath_posix = Path(tpath).as_posix()
            if tpath_posix not in paths:
                paths.append(tpath_posix)
        if not paths:
            return
        _git_run(["add", "--", *paths], cwd=".")
        if _git_run(["diff", "--cached", "--quiet"], cwd=".").returncode == 0:
            return  # nothing staged
        slug = Path(spec_path).stem if spec_path else Path(tpath).stem
        _git_run(["commit", "-m", f"docs: add {slug} design and task DAG"], cwd=".")

    if not _os.environ.get("SUPER_AGENT_TEST") and not dry_run:
        _commit_design_and_tasks()

    order = topo_order(tasks)
    by_id = {t["task_id"]: t for t in tasks}
    results: list[dict] = []
    results_by_id: dict[str, dict] = {}

    def _norm_path_str(p: str) -> str:
        if not p:
            return ""
        try:
            return str(Path(p).resolve())
        except (OSError, ValueError):
            return p

    def _resumable_task_ids() -> dict[str, dict]:
        """task_id -> {"commit": ...} for tasks that already have a confirmed
        `integrated` event for this (design_file, task_file) in the ledger,
        whose commit is still an ancestor of target_branch.

        Best-effort: any failure (unreadable ledger, git not available, seq a
        test double without a usable load_flat()) is swallowed and treated as
        "nothing resumable" so drive falls back to its previous behavior
        (re-run everything) rather than silently skipping work it can't
        actually confirm. Restricted to non-speculative mode: the `integrated`
        event's task_id is the plain task_id only in the single-channel path;
        in speculative mode it is a composite channel id (T1__vendor_0) that
        can't be mapped back to the base task_id reliably.
        """
        if not resume or seq is None or speculative:
            return {}
        wanted_design = _norm_path_str(spec_path or "")
        wanted_task_file = _norm_path_str(task_file or str(tasks_file.resolve()))
        try:
            latest: dict[str, dict] = {}
            for ev in seq.load_flat():
                if ev.get("type") != "integrated":
                    continue
                if _norm_path_str(ev.get("design_file", "")) != wanted_design:
                    continue
                if wanted_task_file and _norm_path_str(ev.get("task_file", "")) != wanted_task_file:
                    continue
                event_id = ev.get("event_id", "")
                tid = event_id.split(":", 1)[0] if event_id else ""
                if tid:
                    latest[tid] = ev  # later events (later in the stream) win
            confirmed: dict[str, dict] = {}
            for tid, ev in latest.items():
                commit = ev.get("commit")
                if not commit:
                    continue
                r = _git_run(["merge-base", "--is-ancestor", commit, target_branch], cwd=".")
                if r.returncode == 0:
                    confirmed[tid] = {"commit": commit}
            return confirmed
        except Exception:
            return {}

    def _synthetic_skip_entry(tid: str, info: dict) -> dict:
        return {
            "task_id": tid,
            "implement": {"skipped": "already_integrated"},
            "review": {"skipped": "already_integrated"},
            "integrate": {"ok": True, "commit": info.get("commit"),
                          "skipped": True, "reason": "already_integrated"},
        }

    resumable = _resumable_task_ids()

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

    # Apply CLI --model/--effort overrides to every implement channel (they take
    # precedence over the yaml/role defaults). implement() also runs a code-side
    # normalize_model() over each channel's model, so a known alias in vendors.yaml
    # (e.g. `hy3:Free` -> `tencent/hy3:free`) resolves to a live id even without an
    # explicit override.
    for ch in channels:
        if implement_model is not None:
            ch["model"] = implement_model
        if implement_effort is not None:
            ch["effort"] = implement_effort
        if implement_timeout is not None:
            ch["timeout"] = implement_timeout

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
                cw = create_worktree(tid, root="workspaces", dry_run=dry_run,
                                      design_file=spec_path or "")
                wt = str(Path(cw["path"]).resolve())
                if not cw.get("ok"):
                    return {"vendor": ch["vendor"], "model": ch["model"],
                            "effort": ch["effort"], "task_id": tid,
                            "ok": False, "error": cw.get("error", "worktree create failed")}
                impl = implement(tid, task, wt, vendor=ch["vendor"],
                                 model=ch["model"], effort=ch["effort"],
                                 seq=seq, dry_run=dry_run,
                                 design_file=spec_path or "",
                                 design_context=spec_text,
                                 timeout=ch.get("timeout"))
                ch_results.append({"vendor": ch["vendor"], "model": ch["model"],
                                   "effort": ch["effort"], "worktree": wt,
                                   "task_id": tid, "impl": impl, "ok": True})
                channel_ids.append(tid)
            else:
                # Multi-channel: one worktree/branch per (task, channel).
                def _run_channel(i: int, ch: dict) -> dict:
                    cid = _channel_worktree_id(tid, ch["vendor"], i)
                    cw = create_worktree(cid, root="workspaces", dry_run=dry_run,
                                          design_file=spec_path or "")
                    wt = str(Path(cw["path"]).resolve())
                    if not cw.get("ok"):
                        return {"vendor": ch["vendor"], "model": ch["model"],
                                "effort": ch["effort"], "task_id": cid,
                                "ok": False, "error": cw.get("error", "worktree create failed")}
                    impl = implement(cid, task, wt, vendor=ch["vendor"],
                                     model=ch["model"], effort=ch["effort"],
                                     seq=seq, dry_run=dry_run,
                                     design_file=spec_path or "",
                                     design_context=spec_text,
                                     timeout=ch.get("timeout"))
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
                    {
                        "vendor": c["vendor"],
                        "model": c["model"],
                        "effort": c["effort"],
                        "ok": c.get("impl", {}).get("ok", False) if c.get("ok") else False,
                        "commit": c.get("impl", {}).get("commit") if c.get("ok") else None,
                        "error": c.get("impl", {}).get("error") if c.get("ok") else c.get("error"),
                    }
                    for c in ch_results
                ]
            }

            # ⑤⑥⑦ review — each channel, pick the first that passes
            acc = _resolve_acceptance(task)
            rev_role = resolve_role("review", config_dir)
            rev_vendor = reviewer_vendor or rev_role["vendor"]
            winner: dict | None = None
            fallback_winner: dict | None = None
            reviews: list[dict] = []
            for c in ch_results:
                impl_ok = c.get("ok") and c.get("impl", {}).get("ok")
                if not impl_ok:
                    # channel worktree or implement failed — record error and skip review
                    err = c.get("error") or (c.get("impl", {}).get("error") if c.get("ok") else "implement failed")
                    reviews.append({"vendor": rev_vendor, "verdict": None, "error": err})
                    continue
                rev = run_pipeline(c["task_id"], c["worktree"], acc,
                                   reviewer_vendor=rev_vendor,
                                   dry_run=dry_run, seq=seq,
                                   model=rev_role["model"], effort=rev_role["effort"],
                                   design_file=spec_path or "",
                                   timeout=rev_role.get("timeout"))
                verdict = rev.get("verdict")
                # Record the REVIEWER vendor (rev_vendor), not the implementer's
                # vendor — this entry is the review phase, and the actual
                # reviewer.invoked event uses rev_vendor (e.g. agy from yaml).
                reviews.append({"vendor": rev_vendor, "verdict": verdict})
                if winner is None and verdict in ("pass", "pass_with_findings"):
                    winner = c
                if fallback_winner is None and c.get("impl", {}).get("commit") and verdict == "judgment_unavailable":
                    fallback_winner = c

            if winner is None and fallback_winner is not None:
                winner = fallback_winner

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
            import traceback as _tb
            entry["error"] = _tb.format_exc()
            entry["_winner"] = None
            entry["_channel_ids"] = channel_ids
            entry["review"] = {"channels": [], "judgment_unavailable": True,
                               "verdict": None, "error": str(ex)}
            return entry

    def _integrate_and_record(tid: str, entry: dict) -> None:
        """Phase B for ONE task: integrate its winning channel (if any) into
        target_branch, tear down all its channel worktrees, and append the
        finished entry to `results`. Callers must invoke this serially
        (git checkout/merge on the shared repo root must never run
        concurrently), and only after the task's dependencies have already
        been integrated here — see docs/plans/drive-resume.md Phase 0."""
        task = by_id.get(tid, {})
        winner = entry.pop("_winner", None)
        channel_ids = entry.pop("_channel_ids", [tid])
        if not dry_run and winner is not None:
            try:
                integ = integrate(winner["task_id"], task, winner["worktree"],
                                 target_branch=target_branch, seq=seq, dry_run=dry_run,
                                 design_file=spec_path or "", all_tasks=tasks)
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
                teardown_worktree(cid, root="workspaces", dry_run=dry_run,
                                   design_file=spec_path or "")
        results.append(entry)

    def _run_and_integrate(tid: str) -> None:
        """Resume (skip) an already-integrated task, or run the full
        implement->review->integrate pipeline for one task."""
        if tid in resumable:
            entry = _synthetic_skip_entry(tid, resumable[tid])
            results_by_id[tid] = entry
            results.append(entry)
            return
        entry = _run_task_pipeline(tid)
        results_by_id[tid] = entry
        _integrate_and_record(tid, entry)

    # Phase A+B, interleaved PER TOPO LAYER: each layer's implement+review runs
    # concurrently (task-level parallelism), then that layer's winners are
    # integrated (serially) BEFORE the next layer starts. This ensures a later
    # layer's tasks are implemented against a worktree that already contains
    # their dependencies' merged output (see docs/plans/drive-resume.md Phase 0
    # — previously integrate ran only once, after ALL layers, so a downstream
    # task could be implemented/reviewed before its dependency was merged).
    if parallel_tasks:
        layers = topo_layers(tasks)
        # Use a while-loop (not `for layer in layers`) because the planner may
        # REVISE `tasks` mid-flight (merge over-split tasks, carve investigation
        # tasks). We re-derive `layers` after each replan and restart the loop
        # from the front so the revised DAG is actually executed — a `for`
        # loop would keep iterating the stale original `layers` and silently
        # ignore the planner's merge.
        idx = 0
        while idx < len(layers):
            layer = layers[idx]
            idx += 1
            # --- Adaptive re-planning (planner) BEFORE this layer ---
            if adaptive and seq is not None:
                events = seq.load()
                planner_role_defaults = resolve_role("planner", config_dir)
                rep = planner_role.replan(
                    requirement, tasks, events=events,
                    vendor=planner_role_defaults["vendor"],
                    existing_design=spec_text,
                    model=planner_role_defaults.get("model"),
                    seq=seq, dry_run=dry_run,
                    design_file=spec_path or "",
                    timeout=planner_role_defaults.get("timeout"),
                )
                if rep.get("ok") and rep.get("tasks"):
                    new_tasks = rep["tasks"]
                    if [t["task_id"] for t in new_tasks] != [t["task_id"] for t in tasks]:
                        # DAG changed: adopt it and RESTART the layer loop from
                        # the front with the revised layers so nothing stale runs.
                        tasks = new_tasks
                        by_id = {t["task_id"]: t for t in tasks}
                        order = topo_order(tasks)
                        layers = topo_layers(tasks)
                        idx = 0
                        continue
                    tasks = new_tasks
                    by_id = {t["task_id"]: t for t in tasks}
                    order = topo_order(tasks)
                # Investigation tasks run FIRST (before this layer's real work).
                for it in rep.get("investigation_needed", []):
                    itid = it.get("task_id", "investigate")
                    if itid in by_id and itid not in layer:
                        _run_and_integrate(itid)
            with ThreadPoolExecutor(max_workers=max_task_workers) as ex:
                run_tids = [tid for tid in layer if tid not in resumable]
                layer_entries = list(ex.map(_run_task_pipeline, run_tids)) if run_tids else []
            for entry in layer_entries:
                results_by_id[entry["task_id"]] = entry
                _integrate_and_record(entry["task_id"], entry)
            for tid in layer:
                if tid in resumable:
                    entry = _synthetic_skip_entry(tid, resumable[tid])
                    results_by_id[tid] = entry
                    results.append(entry)
    else:
        for tid in order:
            _run_and_integrate(tid)

    # Push target_branch to origin (opt-in): once, after every task has
    # finished, while still checked out on target_branch — before restoring
    # the caller's branch below.
    push_result: dict | None = None
    if push and not dry_run and not _os.environ.get("SUPER_AGENT_TEST"):
        p = _git_run(["push", "-u", "origin", target_branch], cwd=".")
        push_result = {"ok": p.returncode == 0}
        if p.returncode != 0:
            push_result["error"] = (p.stderr or p.stdout).strip()[:500]

    # Restore the caller's branch so drive() doesn't leave the repo on the
    # target branch as a side effect. Also pop any auto-stash we created.
    # If we never left the target branch (caller was already on it), there is
    # nothing to restore and no stash to pop.
    _restore_branch()
    _restore_docs_snapshot()
    out = {"ok": True, "reused_tasks_file": reused, "tasks": results}
    if push_result is not None:
        out["push"] = push_result
    return out


def uuid_short() -> str:
    import uuid
    return f"T-{uuid.uuid4().hex[:8]}"


def render_tasks_md_safe(tasks: list[dict], requirement: str, path: Path) -> None:
    try:
        from harness.roles.decomposer import render_tasks_md
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_tasks_md(tasks, requirement), encoding="utf-8")
    except Exception:
        pass
