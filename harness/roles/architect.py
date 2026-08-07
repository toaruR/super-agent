#!/usr/bin/env python
"""Architect role (Stage 1, §9 step ①).

Records design decisions to the ledger as ADRs (Architecture Decision Records).
- Human-supplied design: `architect "<req>" --spec <file>` loads the file and
  records it verbatim as an ADR.
- LLM-proposed design: `architect "<req>"` asks a read-only vendor to propose
  decisions; the structured output is recorded as ADRs.

All decisions land on the ledger as `adr.written` events (H3-safe via Sequencer),
so "what we decided and why" is auditable independently of the code.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.core.invoke import invoke, load_vendors

# Structured output we ask the vendor for (read-only proposal).
ADR_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "decision": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["topic", "decision"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decisions"],
}

ARCHITECT_PROMPT = """あなたはシステムアーキテクトです。要求と既存の設計文書を読み、
この要求を満たすために必要な「設計決定（ADR）」を提案してください。

ルール:
- 実装は行わない（推論・方式選定のみ）。
- 各決定は topic / decision / rationale を持つこと。
- 曖昧な点は open_questions に挙げること（人間に確認させる）。

要求: {requirement}
既存の設計: {existing}
"""


def propose(task_id: str, requirement: str, vendor: str, spec_path: str | None = None,
            existing_design: str = "", dry_run: bool = False,
            seq=None) -> dict:
    """Propose/record design decisions. Returns the ADR payload.

    ledger events written: adr.written (topic/decision/rationale per decision).
    """
    emit = (lambda tid, typ, **kw: seq.propose(tid, typ, **kw)) if seq is not None \
        else (lambda tid, typ, **kw: None)  # standalone: no ledger

    if spec_path:
        text = Path(spec_path).read_text(encoding="utf-8", errors="ignore")
        adr = {"source": "human", "decisions": [{"topic": Path(spec_path).name,
                                                  "decision": text, "rationale": ""}]}
    else:
        decls = load_vendors(Path(__file__).resolve().parent.parent / "config")
        decl = decls.get(vendor, decls["claude"])
        prompt = ARCHITECT_PROMPT.format(requirement=requirement, existing=existing_design)
        if dry_run:
            res = invoke(decl, prompt, schema=ADR_SCHEMA, dry_run=True)
            return {"source": "llm(dry)", "cmd": res.get("cmd"), "decisions": []}
        res = invoke(decl, prompt, schema=ADR_SCHEMA, dry_run=False)
        parsed = res.get("result") or {}
        adr = {"source": "llm", "decisions": parsed.get("decisions", []),
               "open_questions": parsed.get("open_questions", [])}

    # record each decision as its own adr.written event
    for d in adr.get("decisions", []):
        emit(task_id, "adr.written", topic=d.get("topic", ""),
              decision=d.get("decision", ""), rationale=d.get("rationale", ""))
    return adr
