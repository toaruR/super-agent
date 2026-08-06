#!/usr/bin/env python
"""Adjudicator: machine-only. Implements ARCHITECTURE.md 7.2 ruling table.
Takes CVE evidence + reviewer opinion, emits the BINDING verdict.
The reviewer's opinion_verdict is deliberately ignored."""
import json, sys

def adjudicate(cve_ok, cve_probe_ok, reviewer_json, reviewer_available=True):
    if not cve_probe_ok:
        return {"verdict": "environment_error", "reason": "CVE probe failed", "cites": []}
    if not reviewer_available:
        return {"verdict": "judgment_unavailable", "reason": "reviewer invocation failed", "cites": []}
    if not cve_ok:
        return {"verdict": "fail", "reason": "CVE evidence shows failure", "cites": ["E-991"]}
    # evidence passed -> examine findings, discarding uncited ones (P3)
    findings = reviewer_json.get("findings", [])
    cited = [f for f in findings if f.get("cites")]
    discarded = len(findings) - len(cited)
    if not cited:
        return {"verdict": "pass", "reason": f"evidence exit 0; {discarded} uncited finding(s) discarded",
                "cites": ["E-991"], "discarded": discarded}
    return {"verdict": "pass_with_findings",
            "reason": "evidence exit 0; cited findings filed as follow-up tasks",
            "cites": ["E-991"], "followups": [f["claim"] for f in cited], "discarded": discarded}

if __name__ == "__main__":
    rev = json.load(open(sys.argv[1]))
    cve_ok = sys.argv[2] == "0"
    out = adjudicate(cve_ok=cve_ok, cve_probe_ok=True, reviewer_json=rev)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print("\n--- reviewer's own opinion was:", rev.get("opinion_verdict"), "(NOT used for the ruling) ---")
