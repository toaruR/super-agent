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

import re


def _normalize_findings(parsed: dict) -> dict:
    """Ensure `findings` is a list and populate it from alternative keys like
    `findings_and_issues`, `areas_for_improvement`, or `recommendations` if `findings` is empty."""
    findings = parsed.get("findings")
    if not isinstance(findings, list):
        findings = []

    if not findings:
        raw_items = (
            parsed.get("findings_and_issues") or
            parsed.get("areas_for_improvement") or
            parsed.get("issues") or
            parsed.get("recommendations") or
            []
        )
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    cites = item.get("cites") or []
                    if not isinstance(cites, list):
                        cites = [str(cites)]
                    sev = item.get("severity") or item.get("impact") or "medium"
                    desc = item.get("description") or item.get("summary") or item.get("issue") or str(item)
                    remedy = item.get("remediation") or item.get("remedy") or ""
                    if remedy:
                        desc = f"{desc} (Remedy: {remedy})"
                    findings.append({"cites": cites, "severity": str(sev), "summary": str(desc)})
                elif isinstance(item, str):
                    findings.append({"cites": [], "severity": "medium", "summary": item})

    parsed["findings"] = findings
    return parsed


def _parse_markdown_review(text: str) -> dict | None:
    """Parse a plain Markdown code review report into a structured findings dictionary
    when the vendor LLM returns human-readable Markdown instead of JSON Schema output."""
    if not text or not isinstance(text, str):
        return None

    if not any(k in text for k in ("Code Review", "Findings", "Executive Summary", "####", "###")):
        return None

    findings = []
    sections = re.split(r"\n(?=#{2,4}\s+)", text)
    for sec in sections:
        sec_str = sec.strip()
        if not sec_str:
            continue
        lines = sec_str.splitlines()
        header = lines[0].lstrip("#").strip()

        if any(h in header.lower() for h in ("executive summary", "omitted material", "table of contents")):
            continue

        cites = sorted(list(set(re.findall(r"\bE-\d+\b", sec_str))))
        clean_header = re.sub(r"^\d+\.\s*", "", header)

        desc_lines = []
        for line in lines[1:]:
            line_str = line.strip()
            if line_str.startswith("* **Finding**:") or line_str.startswith("**Finding**:") or line_str.startswith("Finding:"):
                desc_lines.append(re.sub(r"^\*\s*\*\*Finding\*\*:\s*", "", line_str))
            elif line_str.startswith("* ") or line_str.startswith("- "):
                desc_lines.append(line_str.lstrip("*- ").strip())

        desc = " ".join(desc_lines) if desc_lines else clean_header
        summary = f"{clean_header}: {desc}" if desc != clean_header else clean_header

        findings.append({
            "cites": cites,
            "severity": "medium",
            "summary": summary[:300]
        })

    if not findings:
        ev_ids = sorted(list(set(re.findall(r"\bE-\d+\b", text))))
        findings.append({
            "cites": ev_ids,
            "severity": "medium",
            "summary": text[:200].replace("\n", " ").strip()
        })

    return {"findings": findings}


def _extract_json_review(res: dict) -> dict | None:
    """Fallback parser to extract structured JSON from raw text or markdown code blocks
    when the reviewer vendor returns plain text instead of strict JSON Schema output."""
    if not isinstance(res, dict):
        return None
    result = res.get("result")
    if isinstance(result, dict):
        return _normalize_findings(result)

    raw_texts = []
    if isinstance(result, str):
        raw_texts.append(result)
    for k in ("raw", "output", "stdout"):
        v = res.get(k)
        if isinstance(v, str):
            raw_texts.append(v)

    for text in raw_texts:
        if not text:
            continue
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = m.group(1) if m else None
        if not candidate:
            m = re.search(r"(\{.*\})", text, re.DOTALL)
            candidate = m.group(1) if m else None
        if candidate:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return _normalize_findings(parsed)
            except Exception:
                pass

        # If no JSON candidate could be parsed, attempt Markdown report parsing
        md_parsed = _parse_markdown_review(text)
        if md_parsed and md_parsed.get("findings"):
            return md_parsed

    return None


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
    design_file: str = "",
    timeout: int | None = None,
) -> dict:
    """Run the full verification pipeline for one task. Returns the judgment.

    ledger events written: verification.run, reviewer.invoked, judgment.
    If seq is None, events are written directly (standalone use).
    """
    worktree = Path(worktree)
    ledger = seq._ledger if seq is not None else Ledger(str(CONFIG_DIR.parent / "ledger" / "events.jsonl"))
    if seq is not None:
        emit = lambda tid, typ, **kw: seq.propose(tid, typ, design_file=design_file, **kw)
    else:
        emit = lambda tid, typ, **kw: ledger.append_event(design_file, "", {"event_id": f"{tid}:0", "type": typ, **kw})

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
    try:
        from harness.core.progress import write_progress
        from harness.cli import auto_update_dashboard
        ledger_path = str(seq.path if seq is not None else CONFIG_DIR.parent / "ledger" / "events.jsonl")
        write_progress(task_id, ledger_path, vendor=reviewer_vendor, status="reviewing", detail="reviewing task...")
        auto_update_dashboard()
    except Exception:
        pass

    if dry_run:
        # record intent, skip live call
        emit(task_id, "reviewer.skipped", reason="dry_run")
        review = None
    else:
        invoke_kwargs = {"timeout": timeout} if timeout is not None else {}
        res = invoke(reviewer, brief_text, schema=REVIEW_SCHEMA,
                     worktree=str(worktree), model=model, effort=effort, role="design", dry_run=False,
                     **invoke_kwargs)
        try:
            review = res.get("result")
            if not isinstance(review, dict) or "findings" not in review:
                review = _extract_json_review(res)
        except Exception:
            review = _extract_json_review(res)
        emit(task_id, "reviewer.raw", returncode=res.get("returncode"))

    # 4) Adjudicate (evidence-only; independent of reviewer's environment)
    judgment = adjudicate(evidence, review)
    if judgment.get("verdict") == "pass":
        emit(task_id, "review.pass")
    else:
        emit(task_id, "review.fail", why=judgment.get("why", ""))
    emit(task_id, "judgment", verdict=judgment["verdict"], why=judgment["why"],
         tree_hash=judgment["tree_hash"], n_advisory=len(judgment.get("advisory", [])))
    try:
        from harness.cli import auto_update_dashboard
        auto_update_dashboard()
    except Exception:
        pass
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
