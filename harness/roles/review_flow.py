#!/usr/bin/env python
"""Review pipeline orchestrator (Stage C).

Connects the verified building blocks under ledger-driven control:
  CVE verify  ->  brief build  ->  reviewer (read-only, other vendor)
              ->  adjudicate  ->  ledger events

The reviewer is invoked through the adapter (invoke.py) with read-only
permissions; its finding is judged ONLY against CVE evidence (adjudicate),
so the reviewer's own execution environment never enters the verdict (E-4).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness.core.adjudicate import adjudicate
from harness.core.brief import build
from harness.core.cve import CVE
from harness.core.invoke import invoke, load_vendors
from harness.core.ledger import Ledger, Sequencer

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cites": {"type": "array", "items": {"type": "string"}},
                    "severity": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["cites", "severity", "summary"],
            },
        }
    },
    "required": ["findings"],
}


def run_pipeline(
    task_id: str,
    worktree: str | Path,
    acceptance: list[dict],
    reviewer_vendor: str = "codex",
    budget_tokens: int = 4000,
    dry_run: bool = False,
    model: str | None = None,
    effort: str | None = None,
    seq: Sequencer | None = None,
) -> dict:
    """Run the full verification pipeline for one task. Returns the judgment.

    ledger events written: verification.run, reviewer.invoked, judgment.
    If seq is None, events are written directly (standalone use).
    """
    worktree = Path(worktree)
    ledger = seq._ledger if seq is not None else Ledger(str(CONFIG_DIR.parent / "ledger" / "events.jsonl"))
    if seq is not None:
        emit = lambda tid, typ, **kw: seq.propose(tid, typ, **kw)
    else:
        emit = lambda tid, typ, **kw: ledger.append(tid, typ, **kw)

    # 1) CVE verification (the only place anything is executed)
    if dry_run:
        # dry-run: 実行を一切行わず、ダミー証拠を作る（計画のみ出力）
        evidence = {"tree_hash": "dry-run", "cve_ok": True, "evidence": []}
        emit(task_id, "verification.run", cve="dry-run", tree_hash="dry-run",
             cve_ok=True, n_evidence=0)
    else:
        cve = CVE(CONFIG_DIR / "verification_env.yaml", CONFIG_DIR / "verifiers.yaml")
        evidence = cve.run(worktree, acceptance)
        emit(task_id, "verification.run",
             cve="local-win-py311", tree_hash=evidence["tree_hash"],
             cve_ok=evidence["cve_ok"],
             n_evidence=len(evidence["evidence"]))

    if not evidence["cve_ok"]:
        j = adjudicate(evidence, None)
        emit(task_id, "judgment", verdict=j["verdict"], why=j["why"], tree_hash=j["tree_hash"])
        return j

    # 2) Brief (hierarchical degradation; reviewer must NOT execute)
    changed = sorted(worktree.rglob("*.py"))
    context = changed  # minimal: full py set as context tier
    brief_text, stats = build(evidence, changed, context, budget_tokens, worktree)
    emit(task_id, "brief.built", budget=budget_tokens, tokens_est=stats["tokens_est"],
         dropped=stats["dropped"])

    # 3) Reviewer (read-only, different vendor; invoked through adapter)
    decls = load_vendors(CONFIG_DIR)
    reviewer = decls.get(reviewer_vendor, decls["codex"])
    emit(task_id, "reviewer.invoked", vendor=reviewer_vendor)
    if dry_run:
        # record intent, skip live call
        emit(task_id, "reviewer.skipped", reason="dry_run")
        review = None
    else:
        res = invoke(reviewer, brief_text, schema=REVIEW_SCHEMA,
                     worktree=str(worktree), model=model, effort=effort, role="design", dry_run=False)
        try:
            review = res.get("result")
        except Exception:
            review = None
        emit(task_id, "reviewer.raw", returncode=res.get("returncode"))

    # 4) Adjudicate (evidence-only; independent of reviewer's environment)
    judgment = adjudicate(evidence, review)
    emit(task_id, "judgment", verdict=judgment["verdict"], why=judgment["why"],
         tree_hash=judgment["tree_hash"], n_advisory=len(judgment.get("advisory", [])))
    return judgment


if __name__ == "__main__":
    # quick standalone test against caseB
    tid = "T-standalone"
    seq = Sequencer(str(CONFIG_DIR.parent / "ledger" / "events.jsonl"))
    seq.start()
    acc = [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}]
    j = run_pipeline(tid, REPO_ROOT / "probe" / "n3" / "caseB", acc,
                     reviewer_vendor="codex", seq=seq, dry_run=True)
    seq.stop()
    print(json.dumps(j, ensure_ascii=False, indent=2))
