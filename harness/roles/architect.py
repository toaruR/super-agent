#!/usr/bin/env python
"""Architect role (Stage 1, §9 step ①).

Records design decisions to the ledger as ADRs (Architecture Decision Records).
- Human-supplied design: `architect "<req>" --design_file <file>` loads the file and
  records it verbatim as an ADR.
- LLM-proposed design: `architect "<req>"` asks a read-only vendor to propose
  decisions; the structured output is recorded as ADRs.

All decisions land on the ledger as `adr.written` events (H3-safe via Sequencer),
so "what we decided and why" is auditable independently of the code.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.core.invoke import invoke, load_vendors
from harness.core.progress import write_progress

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

def _coerce_parsed(result: object) -> dict:
    """extract_result() may hand back a raw string (vendor answered without
    embedded JSON, e.g. free-text explanation) instead of the parsed dict.
    Try to recover JSON from it; on failure treat as no decisions."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            obj = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


ARCHITECT_PROMPT = """あなたはシステムアーキテクトです。要求と既存の設計文書を読み、
この要求を満たすために必要な「設計決定（ADR）」を提案してください。

ルール:
- 実装は行わない（推論・方式選定のみ）。
- 各決定は topic / decision / rationale を持つこと。
- 曖昧な点は open_questions に挙げること（人間に確認させる）。

出力は必ず以下の形の JSON のみとすること（説明文や前置きは書かない）:
{{"decisions": [{{"topic": "...", "decision": "...", "rationale": "..."}}], "open_questions": ["..."]}}

要求: {requirement}
既存の設計: {existing}
"""


def _invoke_design(decl, prompt: str, *, vendor: str, model: str | None, effort: str | None,
                   dry_run: bool, invoke_kwargs: dict, task_id: str, seq, emit,
                   draft_path: str | None = None) -> dict:
    """Run the read-only design LLM call, wired to the progress side-channel."""
    kwargs = dict(invoke_kwargs)
    if draft_path:
        kwargs["draft_path"] = draft_path

    if dry_run:
        res = invoke(decl, prompt, schema=ADR_SCHEMA, model=model, effort=effort,
                     role="design", dry_run=True, **kwargs)
        return {"dry_run": True, "cmd": res.get("cmd")}

    progress_cb = None
    if seq is not None:
        ledger_path = seq.path

        def progress_cb(detail: str) -> None:
            write_progress(task_id, ledger_path, vendor=vendor,
                           status="running", detail=detail)

        write_progress(task_id, ledger_path, vendor=vendor,
                       status="running", detail="starting architect proposal")

    try:
        res = invoke(decl, prompt, schema=ADR_SCHEMA, model=model, effort=effort,
                     role="design", dry_run=False, progress_cb=progress_cb,
                     **kwargs)
    except FileNotFoundError as e:
        err = str(e)
        emit(task_id, "architect.error", error=err)
        if seq is not None:
            write_progress(task_id, seq.path, vendor=vendor, status="error", detail=err[:200])
        return {"error": err}
    except subprocess.TimeoutExpired as e:
        err = f"vendor subprocess timed out after {e.timeout}s"
        emit(task_id, "architect.error", error=err)
        if seq is not None:
            write_progress(task_id, seq.path, vendor=vendor, status="error", detail=err[:200])
        return {"error": err}

    if seq is not None:
        write_progress(task_id, seq.path, vendor=vendor, status="done", detail="")

    return _coerce_parsed(res.get("result"))


def propose(task_id: str, requirement: str, vendor: str, spec_path: str | None = None,
            existing_design: str = "", dry_run: bool = False,
            seq=None, model: str | None = None, effort: str | None = None,
            timeout: int | None = None) -> dict:
    """Propose/record design decisions. Returns the ADR payload.

    ledger events written: adr.written (topic/decision/rationale per decision),
    architect.error (vendor subprocess failed to start or timed out).
    """
    emit = (lambda tid, typ, **kw: seq.propose(tid, typ, design_file=spec_path or "", **kw)) if seq is not None \
        else (lambda tid, typ, **kw: None)  # standalone: no ledger
    invoke_kwargs = {"timeout": timeout} if timeout is not None else {}

    if spec_path:
        p = Path(spec_path)
        draft_p = Path(f"{spec_path}.draft")
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="ignore")
            adr = {"source": "human", "design_file": str(p), "saved_to": str(p),
                   "decisions": [{"topic": p.name, "decision": text, "rationale": ""}]}
        else:
            if draft_p.exists():
                draft_text = draft_p.read_text(encoding="utf-8", errors="ignore")
                notice = (
                    f"\n\n【前回の試行で途中まで作成された設計ドラフト（引き継ぎ用）】\n"
                    f"---\n{draft_text}\n---\n"
                    f"前回の検討内容を検証・補足・完結させ、重複を避けつつ正しいフォーマットの JSON を出力してください。"
                )
                existing_design = (existing_design + notice) if existing_design else notice

            # ファイルが無い → LLM で起案してそのパスに保存（A 方針: シームレス作成）
            decls = load_vendors(Path(__file__).resolve().parent.parent / "config")
            decl = decls.get(vendor, decls["claude"])
            prompt = ARCHITECT_PROMPT.format(requirement=requirement, existing=existing_design)
            result = _invoke_design(decl, prompt, vendor=vendor, model=model, effort=effort,
                                    dry_run=dry_run, invoke_kwargs=invoke_kwargs,
                                    task_id=task_id, seq=seq, emit=emit,
                                    draft_path=str(draft_p))
            if result.get("dry_run"):
                return {"source": "llm(dry)", "cmd": result.get("cmd"), "decisions": []}
            if "error" in result:
                return {"source": "llm(error)", "error": result["error"], "decisions": []}
            decisions = result.get("decisions", [])
            # 起案結果をファイルに保存（人間が後で編集・参照できるよう）
            body = "\n\n".join(
                f"## {d.get('topic', 'decision')}\n\n{d.get('decision', '')}\n\n"
                f"理由: {d.get('rationale', '')}" for d in decisions
            )
            open_questions = result.get("open_questions", [])
            if open_questions:
                body += "\n\n## 未解決の問い\n" + "\n".join(f"- {q}" for q in open_questions)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# 設計: {requirement}\n\n{body}\n", encoding="utf-8")
            if draft_p.exists():
                try:
                    draft_p.unlink()
                except OSError:
                    pass
            adr = {"source": "llm->file", "saved_to": str(p),
                   "decisions": decisions, "open_questions": open_questions}
    else:
        decls = load_vendors(Path(__file__).resolve().parent.parent / "config")
        decl = decls.get(vendor, decls["claude"])
        prompt = ARCHITECT_PROMPT.format(requirement=requirement, existing=existing_design)
        result = _invoke_design(decl, prompt, vendor=vendor, model=model, effort=effort,
                                dry_run=dry_run, invoke_kwargs=invoke_kwargs,
                                task_id=task_id, seq=seq, emit=emit)
        if result.get("dry_run"):
            return {"source": "llm(dry)", "cmd": result.get("cmd"), "decisions": []}
        if "error" in result:
            return {"source": "llm(error)", "error": result["error"], "decisions": []}
        adr = {"source": "llm", "decisions": result.get("decisions", []),
               "open_questions": result.get("open_questions", [])}

    # record each decision as its own adr.written event
    for d in adr.get("decisions", []):
        emit(task_id, "adr.written", topic=d.get("topic", ""),
              decision=d.get("decision", ""), rationale=d.get("rationale", ""))
    return adr
