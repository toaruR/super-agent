#!/usr/bin/env python
"""Adjudicator v2. Machine-only. No LLM.

Changes from v1, driven by the independent review:
 - findings without a citation are no longer silently discarded; they are kept
   as `advisory` and surfaced to the human, but never affect the verdict.
 - the verdict is bound to the tree_hash the CVE measured (H4).
"""
from __future__ import annotations

import json
import re
import sys

EV_ID = re.compile(r"\bE-\d+\b")


def adjudicate(evidence: dict, review: dict) -> dict:
    if not evidence.get("cve_ok", False):
        return {"verdict": "environment_error", "why": "CVE probe failed",
                "tree_hash": evidence.get("tree_hash"), "advisory": []}
    if review is None:
        return {"verdict": "judgment_unavailable", "why": "reviewer produced no parseable output",
                "tree_hash": evidence.get("tree_hash"), "advisory": []}

    ev_ids = {e["id"] for e in evidence.get("evidence", [])}
    failed = [e["id"] for e in evidence["evidence"] if e.get("exit_code") != 0]

    cited, advisory = [], []
    for f in review.get("findings", []):
        refs = {c for c in f.get("cites", []) if EV_ID.fullmatch(c.strip())}
        if refs & ev_ids:
            cited.append(f)
        else:
            advisory.append(f)

    if failed:
        return {"verdict": "fail", "why": f"acceptance failed: {failed}",
                "tree_hash": evidence["tree_hash"], "advisory": advisory}
    if cited:
        return {"verdict": "pass_with_findings", "why": "all green; evidence-backed findings",
                "tree_hash": evidence["tree_hash"], "findings": cited, "advisory": advisory}
    return {"verdict": "pass", "why": "all acceptance green",
            "tree_hash": evidence["tree_hash"], "advisory": advisory}


if __name__ == "__main__":
    ev = json.load(open(sys.argv[1], encoding="utf-8"))
    rv = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None
    print(json.dumps(adjudicate(ev, rv), ensure_ascii=False, indent=2))
