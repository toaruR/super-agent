#!/usr/bin/env python
"""Decomposer role (Stage 2, §9 step ②).

Turns a requirement (or an existing design) into a task DAG with acceptance
criteria, then runs the §6.2 structural acceptance contract before recording
anything on the ledger.

Structural checks (machine-enforced, before any LLM re-prompt):
  - acceptance[] is non-empty            (no unverifiable tasks)
  - every acceptance[].verb in verifiers.yaml  (H2: no injection path)
  - acceptance[].args is a list (executable form)
  - DAG has no cycles
  - touch_allow sets do not overlap across parallel tasks
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.core.invoke import invoke, load_vendors
from harness.core.verifiers import VerifierRegistry

DECOMPOSE_SCHEMA = {
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
        }
    },
    "required": ["tasks"],
}

DECOMPOSE_PROMPT = """あなたはタスク分解担当です。要求（と既存の設計）から、実装タスクの DAG を作ってください。

ルール:
- 各タスクは acceptance（検証方法）を持つこと。acceptance は verb+args の形。
  verb は以下のいずれか: {verbs}
- 検証できないタスクは作らない（acceptance は空にしない）。
- depends_on で依存を明示（DAG）。循環は作らない。
- touch_allow は「このタスクが触ってよいファイル」を列挙（パス単位）。

要求: {requirement}
既存の設計: {existing}
"""


def _check_verbs(tasks: list[dict], registry: VerifierRegistry) -> list[str]:
    errs = []
    for t in tasks:
        for a in t.get("acceptance", []):
            verb = a.get("verb", "")
            if registry.resolve({"verb": verb, "args": a.get("args", [])}, ".") is None:
                errs.append(f"{t['task_id']}: verb '{verb}' は verifiers に未登録")
            if not isinstance(a.get("args"), list):
                errs.append(f"{t['task_id']}: acceptance.args がリストではない")
    return errs


def _check_dag(tasks: list[dict]) -> list[str]:
    """Detect cycles via DFS."""
    errs = []
    ids = {t["task_id"] for t in tasks}
    adj = {t["task_id"]: list(t.get("depends_on", [])) for t in tasks}
    # unknown dependency
    for t in tasks:
        for d in t.get("depends_on", []):
            if d not in ids:
                errs.append(f"{t['task_id']}: depends_on '{d}' が存在しない")
    # cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in adj}
    def dfs(u, stack):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                errs.append(f"DAG 循環: {' -> '.join(stack + [u, v])}")
            elif color.get(v, WHITE) == WHITE:
                dfs(v, stack + [u])
        color[u] = BLACK
    for tid in adj:
        if color[tid] == WHITE:
            dfs(tid, [])
    return errs


def _check_touch_overlap(tasks: list[dict]) -> list[str]:
    """Parallel tasks (no depends_on link) must not share touch_allow paths."""
    errs = []
    for i in range(len(tasks)):
        for j in range(i + 1, len(tasks)):
            a, b = tasks[i], tasks[j]
            if set(a.get("touch_allow", [])) & set(b.get("touch_allow", [])):
                if b["task_id"] not in a.get("depends_on", []) and \
                   a["task_id"] not in b.get("depends_on", []):
                    errs.append(
                        f"touch_allow 重複: {a['task_id']} と {b['task_id']} "
                        f"(並列だが範囲が被る)")
    return errs


def structural_check(tasks: list[dict], registry: VerifierRegistry) -> list[str]:
    """Return a list of error strings (empty = pass)."""
    errs = []
    if not tasks:
        errs.append("タスクが0件")
    for t in tasks:
        if not t.get("acceptance"):
            errs.append(f"{t['task_id']}: acceptance が空（検証不能なタスク）")
    errs += _check_verbs(tasks, registry)
    errs += _check_dag(tasks)
    errs += _check_touch_overlap(tasks)
    return errs


def decompose(task_id: str, requirement: str, vendor: str = "claude",
              existing_design: str = "", dry_run: bool = False,
              seq=None) -> dict:
    """Decompose a requirement into a checked task DAG. Returns the payload.

    ledger events: task.created per task (after structural check passes).
    If the check fails, returns {"ok": False, "errors": [...]} and records nothing.
    """
    config_dir = Path(__file__).resolve().parent.parent / "config"
    registry = VerifierRegistry(config_dir / "verifiers.yaml")
    verbs = ", ".join(sorted(registry._map.keys()))

    if dry_run:
        decls = load_vendors(config_dir)
        decl = decls.get(vendor, decls["claude"])
        prompt = DECOMPOSE_PROMPT.format(requirement=requirement, existing=existing_design, verbs=verbs)
        res = invoke(decl, prompt, schema=DECOMPOSE_SCHEMA, dry_run=True)
        return {"ok": True, "dry_run": True, "cmd": res.get("cmd")}

    decls = load_vendors(config_dir)
    decl = decls.get(vendor, decls["claude"])
    prompt = DECOMPOSE_PROMPT.format(requirement=requirement, existing=existing_design, verbs=verbs)
    res = invoke(decl, prompt, schema=DECOMPOSE_SCHEMA, dry_run=False)
    parsed = res.get("result") or {}
    tasks = parsed.get("tasks", [])

    errs = structural_check(tasks, registry)
    if errs:
        if seq is not None:
            seq.propose(task_id, "decompose.rejected", errors=errs)
        return {"ok": False, "errors": errs, "tasks": tasks}

    if seq is not None:
        seq.propose(task_id, "decompose.ok", n_tasks=len(tasks))
        for t in tasks:
            seq.propose(t["task_id"], "task.created",
                        goal=t.get("goal", ""),
                        acceptance=t.get("acceptance", []),
                        depends_on=t.get("depends_on", []),
                        touch_allow=t.get("touch_allow", []))
    return {"ok": True, "tasks": tasks}
