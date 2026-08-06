#!/usr/bin/env python
"""Briefing builder with a hard token budget.

Answers the large-diff problem: when the material exceeds the budget, what do
you drop? Order matters - drop the least decision-relevant thing first.

Tier order (never drop a lower tier to keep a higher one):
  T0 evidence tail   - the failing output. Without it there is no adjudication.
  T1 changed hunks   - what this task actually did.
  T2 signatures      - shape of untouched code the hunks call into.
  T3 full sources    - luxury. First to go.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

CHARS_PER_TOKEN = 4


def toks(s: str) -> int:
    return len(s) // CHARS_PER_TOKEN


def evidence_tail(ev: dict, per_item: int = 1500) -> str:
    out = [f"TREE_HASH: {ev['tree_hash']}", "=== EVIDENCE (CVE-executed) ==="]
    for e in ev["evidence"]:
        body = (e.get("stdout") or "") + (e.get("stderr") or "")
        out.append(f"[{e['id']}] cmd={e['cmd']} exit_code={e['exit_code']}")
        out.append(body[-per_item:] if body else "(no output)")
    return "\n".join(out)


def signatures(path: pathlib.Path) -> str:
    """Public shape only: defs, classes, docstring first line. No bodies."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return f"--- {path.name} (unparseable) ---"
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            lines.append(f"def {node.name}({args})")
        elif isinstance(node, ast.ClassDef):
            lines.append(f"class {node.name}")
    return "\n".join(lines)


def build(ev: dict, changed: list[pathlib.Path], context: list[pathlib.Path],
          budget_tokens: int, root: pathlib.Path) -> tuple[str, dict]:
    header = (
        "ROLE: reviewer (read-only). The CVE already executed everything.\n"
        "Do NOT run commands. Do NOT read files. Judge only from what is below.\n"
        "Every finding MUST cite an evidence id (E-n) or a source line shown here.\n"
        "If material was omitted (see OMITTED), do not guess about it - say so.\n"
    )
    t0 = evidence_tail(ev)
    parts = [header, t0]
    used = toks(header) + toks(t0)
    dropped: list[str] = []

    tier1 = []
    for p in changed:
        body = f"\n--- CHANGED {p.relative_to(root).as_posix()} ---\n" + p.read_text(encoding="utf-8")
        if used + toks(body) > budget_tokens:
            dropped.append(f"changed:{p.name}")
            continue
        tier1.append(body)
        used += toks(body)

    tier2 = []
    for p in context:
        body = f"\n--- SIGNATURES {p.relative_to(root).as_posix()} ---\n" + signatures(p)
        if used + toks(body) > budget_tokens:
            dropped.append(f"sig:{p.name}")
            continue
        tier2.append(body)
        used += toks(body)

    if dropped:
        note = "\n=== OMITTED (budget) ===\n" + ", ".join(dropped)
        parts.append(note)
        used += toks(note)

    brief = "\n".join(parts + tier1 + tier2)
    return brief, {"tokens_est": toks(brief), "budget": budget_tokens, "dropped": dropped}


if __name__ == "__main__":
    root = pathlib.Path(sys.argv[1]).resolve()
    ev = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    budget = int(sys.argv[2])
    changed = sorted(root.glob(sys.argv[3])) if len(sys.argv) > 3 else []
    allpy = sorted(root.rglob("*.py"))
    context = [p for p in allpy if p not in changed]
    brief, stats = build(ev, changed, context, budget, root)
    (root / "brief_budgeted.txt").write_text(brief, encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))
