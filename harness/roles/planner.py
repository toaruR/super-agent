#!/usr/bin/env python3
"""Planner role: adaptive re-planning during Stage B execution.

`design` (decomposer) builds the *initial* static DAG once, before any code
exists. But parallel-ability is often NOT obvious up-front:

  - A task that "should" be independent may actually depend on a shared
    module/interface that only becomes visible after the first task is coded.
  - Some tasks need INVESTIGATION first (read the schema, probe an API) before
    implementation can be planned safely.
  - Completing one task can ENABLE parallelism (its interface is now defined)
    or FORBID it (a blocker was discovered, forcing others to converge).

`replan()` is the "design, but at execution time, with the ledger in hand".
It is invoked by `drive` between topo layers (or when a task fails) and may:
  - carve out INVESTIGATION tasks (so they run first, then real work fans out)
  - revise depends_on / merge over-split tasks (e.g. several tasks all touching
    one file should be one task, not parallel worktrees that can't see each other)
  - re-order so that interface-defining tasks run before their consumers.

The vendor/skill is the same as `design` (claude) but the *responsibility* is
distinct: it reads what actually happened, not just the requirement.

No git mutation here. Records ledger events when seq is given.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.core.invoke import invoke, load_vendors
from harness.roles.decomposer import parse_tasks_md, render_tasks_md

# Same shape as DECOMPOSE_SCHEMA, plus replan-specific outputs.
REPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "goal": {"type": "string"},
                    "acceptance": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "verb": {"type": "string"},
                                "args": {"type": "array", "items": {"type": "string"}},
                                "expect_exit": {"type": "integer"},
                            },
                            "required": ["verb", "args"],
                        },
                    },
                    "touch_allow": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["task_id", "goal", "acceptance"],
            },
        },
        # tasks whose only purpose is to gather info before real work (run first)
        "investigation_needed": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"task_id": {"type": "string"},
                                      "goal": {"type": "string"}}},
        },
        # human-readable rationale for the changes
        "notes": {"type": "string"},
    },
    "required": ["tasks"],
}

REPLAN_PROMPT = """\
あなたは実行中の再計画担当（planner）です。実装がすでに始まっており、これまでの\
実装結果（台帳イベント）があります。初期計画（design の DAG）を見直し、\
「実際に起きたこと」に基づいてタスクを再構成してください。

ルール:
- 各タスクは acceptance（検証方法）を持つこと。acceptance は verb+args の形。
  verb は以下のいずれか: {verbs}
- 検証できないタスクは作らない。
- depends_on で依存を明示（DAG）。循環は作らない。
- touch_allow は「このタスクが触ってよいファイル」を列挙（パス単位）。

【重要】並行処理の可否について以下を判断してください:
1. 調査が必要な箇所は、本実装タスクの「前」に INVESTIGATION タスクとして切り出し、
   investigation_needed に入れてください（これらが先に走る）。
2. 複数のタスクが同じファイルに触る（touch_allow が重なる）場合、それらは独立した
   worktree では互いの成果が見えないため、実際には並行できません。1つのタスクにまとめるか、
   明示的に depends_on で直列化してください。
3. あるタスクが別タスクの「作るべきインタフェース/関数」に依存している場合、
   その依存を depends_on に入れ、並行せず順次にしてください。
4. すでに完了（implemented/integrated）したタスクは、そのまま維持するか、
   後続タスクの依存として参照してください（再実装しない）。

要求: {requirement}

既存の設計: {existing}

現在のタスク DAG:
{current_tasks}

これまでの実装結果（台帳イベント要約）:
{events_summary}

【自動検出: 過分割の疑い（同じファイルに触るタスク群）】
{oversplit_hint}

上記を踏まえて、改訂後のタスク DAG を JSON で出力してください。\
"""


def _summarize_events(events: list[dict]) -> str:
    """Compact ledger summary for the planner prompt."""
    if not events:
        return "（まだ実装イベントなし）"
    lines = []
    for e in events:
        t = e.get("type", "")
        tid = e.get("task_id", "")
        if t in ("task.implemented", "integrated"):
            lines.append(f"- {tid}: 完了({t})")
        elif t in ("implementer.error", "review.failed", "task.blocked"):
            lines.append(f"- {tid}: 失敗({t}) {e.get('error') or e.get('reason') or ''}")
        elif t == "review.verdict":
            lines.append(f"- {tid}: review={e.get('verdict')}")
    return "\n".join(lines) if lines else "（実装イベントなし）"


def _detect_oversplit(tasks: list[dict]) -> str:
    """Heuristic: find groups of tasks that touch the SAME file. Those cannot
    be parallel worktrees (they can't see each other's edits), so they should
    be merged or serialized. Surfaces the hint for the planner prompt."""
    from collections import defaultdict
    by_file: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for f in t.get("touch_allow", []) or []:
            by_file[f].append(t["task_id"])
    groups = {f: tids for f, tids in by_file.items() if len(tids) > 1}
    if not groups:
        return "（なし：各タスクは異なるファイルに触る）"
    lines = []
    for f, tids in groups.items():
        lines.append(f"- {f} を共有: {', '.join(tids)} "
                     f"→ これらは独立 worktree では互いの成果が見えない。"
                     f"1タスクにまとめるか depends_on で直列化を推奨。")
    return "\n".join(lines)


def _merge_oversplit(tasks: list[dict]) -> tuple[list[dict], list[str]]:
    """Auto-merge tasks that share a touch_allow file into one task each.

    Parallel worktrees cannot see each other's edits, so tasks editing the same
    file must NOT run concurrently. We merge them into a single task (goal =
    concatenation, depends_on = union, touch_allow = union). Returns the merged
    task list and a list of human-readable change notes.

    This is a hard rule (not a suggestion) because the failure mode is silent:
    two worktrees editing the same file produce a broken merge that review may
    not catch. Design's up-front DAG can't know this; the planner enforces it
    at execution time.
    """
    from collections import defaultdict
    by_file: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for f in t.get("touch_allow", []) or []:
            by_file[f].append(t["task_id"])
    # groups of task_ids that must be merged (union-find over shared files)
    parent = {t["task_id"]: t["task_id"] for t in tasks}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    file_to_tids: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for f in t.get("touch_allow", []) or []:
            file_to_tids[f].append(t["task_id"])
    for tids in file_to_tids.values():
        for tid in tids[1:]:
            union(tids[0], tid)

    groups: dict[str, list[str]] = defaultdict(list)
    for tid in parent:
        groups[find(tid)].append(tid)

    by_id = {t["task_id"]: t for t in tasks}
    merged: list[dict] = []
    notes: list[str] = []
    for root, tids in groups.items():
        if len(tids) == 1:
            merged.append(by_id[root])
            continue
        # merge the group
        goals = []
        deps: set[str] = set()
        touch: set[str] = set()
        for tid in tids:
            t = by_id[tid]
            goals.append(f"[{tid}] {t.get('goal', '')}")
            deps.update(t.get("depends_on", []) or [])
            touch.update(t.get("touch_allow", []) or [])
        # drop intra-group deps (they are now the same task)
        deps = {d for d in deps if d not in set(tids)}
        # Choose the merge root: prefer the most *foundational* task in the
        # group (one that does not depend on any other task in the group), so
        # the merged task keeps a stable id and doesn't collide with an
        # already-integrated leaf. Fall back to the group's union-find root.
        group_set = set(tids)
        candidates = [tid for tid in tids
                      if not (set(by_id[tid].get("depends_on", []) or []) & group_set)]
        merge_id = candidates[0] if candidates else root
        merged_task = {
            "task_id": merge_id,
            "goal": " / ".join(goals),
            "acceptance": [],
            "depends_on": sorted(deps),
            "touch_allow": sorted(touch),
        }
        merged.append(merged_task)
        notes.append(f"過分割をマージ: {', '.join(tids)} → {merge_id}")
    return merged, notes


def replan(
    requirement: str,
    existing_tasks: list[dict],
    events: list[dict] | None = None,
    vendor: str = "claude",
    existing_design: str = "",
    model: str | None = None,
    seq=None,
    dry_run: bool = False,
) -> dict:
    """Re-plan the task DAG given what actually happened (ledger events).

    Returns {"ok", "tasks", "investigation_needed", "notes"}.
    Records ledger events when seq is given.
    """
    config_dir = Path(__file__).resolve().parent.parent / "config"
    import sys as _sys2
    print(f"DIAG replan ENTER: ids={[t['task_id'] for t in existing_tasks]} events_n={len(events or [])}", file=_sys2.stderr)
    registry = None
    try:
        from harness.core.verify import VerifierRegistry
        registry = VerifierRegistry(config_dir / "verifiers.yaml")
    except Exception:
        registry = None
    verbs = ", ".join(sorted(registry._map.keys())) if registry else ""

    current_tasks_md = render_tasks_md(existing_tasks, requirement)
    events_summary = _summarize_events(events or [])
    oversplit_hint = _detect_oversplit(existing_tasks)
    prompt = REPLAN_PROMPT.format(
        requirement=requirement,
        existing=existing_design,
        verbs=verbs,
        current_tasks=current_tasks_md,
        events_summary=events_summary,
        oversplit_hint=oversplit_hint,
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "prompt": prompt,
            "tasks": existing_tasks,
        }

    decls = load_vendors(config_dir)
    decl = decls.get(vendor, decls["claude"])
    res = invoke(decl, prompt, schema=REPLAN_SCHEMA, model=model, role="planner")

    # invoke may return a string (raw model output) — try to parse JSON.
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except json.JSONDecodeError:
            # find first {...} block
            s = res.find("{")
            e = res.rfind("}")
            if s != -1 and e != -1:
                try:
                    res = json.loads(res[s:e + 1])
                except json.JSONDecodeError:
                    return {"ok": False, "error": "planner returned unparsable output",
                            "raw": res[:500]}

    tasks = res.get("tasks", existing_tasks)
    investigation = res.get("investigation_needed", [])
    notes = res.get("notes", "")

    # Hard rule: never let over-split tasks (sharing a touch_allow file) run as
    # parallel worktrees — merge them so the failure mode (silent broken merge)
    # can't happen. Applied AFTER the vendor so its output is also sanitized.
    merged_tasks, merge_notes = _merge_oversplit(tasks)
    if merge_notes:
        tasks = merged_tasks
        notes = (notes + "\n" if notes else "") + "; ".join(merge_notes)

    # Drop orphan dependencies: the vendor (or merge) may have removed a task
    # that others still depend on. Keep depends_on referencing only tasks that
    # still exist, so the resulting DAG is acyclic and implementable.
    valid_ids = {t["task_id"] for t in tasks}
    for t in tasks:
        t["depends_on"] = [d for d in t.get("depends_on", []) if d in valid_ids]

    if seq is not None:
        seq.propose("replan", "plan.revised",
                    n_tasks=len(tasks),
                    n_investigation=len(investigation),
                    notes=notes[:300])
        for it in investigation:
            seq.propose(it.get("task_id", "investigate"), "task.created",
                        goal=it.get("goal", ""), role="planner",
                        kind="investigation")

    return {
        "ok": True,
        "tasks": tasks,
        "investigation_needed": investigation,
        "notes": notes,
    }


def replan_from_file(
    tasks_path: str,
    requirement: str,
    events: list[dict] | None = None,
    vendor: str = "claude",
    design_path: str = "",
    model: str | None = None,
    seq=None,
    dry_run: bool = False,
) -> dict:
    """Convenience: read tasks from Markdown, re-plan, return revised tasks."""
    tasks = parse_tasks_md(tasks_path)
    design = Path(design_path).read_text(encoding="utf-8", errors="ignore") if design_path else ""
    return replan(requirement, tasks, events=events, vendor=vendor,
                  existing_design=design, model=model, seq=seq, dry_run=dry_run)
