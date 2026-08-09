"""Stage 6 (§9 ⑩): self-improvement via ledger mining (G6).

Reads the event ledger, finds recurring failure patterns, and proposes
upgrades to the acceptance-template catalogue or the constitution. A
proposal is only *recorded* (as a ``design.proposed`` event) when run for
real; ``--dry-run`` shows what would be proposed without touching the ledger.

Reference: docs/plan.md "Stage 6 — 自我改良（evolve）".
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from harness.core.ledger import Ledger, Sequencer

REPO_ROOT = Path(__file__).resolve().parent.parent  # harness/roles -> super-agent/src
LEDGER_PATH = Path(__file__).resolve().parent.parent / "ledger" / "events.jsonl"
CONSTITUTION_PATH = REPO_ROOT / "constitution.md"
ACCEPTANCE_TEMPLATES_PATH = REPO_ROOT / "acceptance-templates.md"

# A failure pattern must recur this many times before we propose an upgrade.
THRESHOLD = 3


def _extract_failures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull failure-shaped events out of the ledger.

    We treat an event as a failure when any of:
      - ``cve_ok`` is present and False
      - ``type`` ends with ``.failed``
      - ``verdict`` is one of the failure verdicts
      - ``returncode`` is non-zero for an execution-shaped event
    Each returned item carries a ``pattern`` key (the signature we group on).
    """
    failures: list[dict[str, Any]] = []
    for ev in events:
        ftype = ev.get("type", "")
        pattern: str | None = None
        reason = ev.get("reason") or ev.get("why") or ""

        if ftype.endswith(".failed"):
            pattern = ftype
        elif "cve_ok" in ev and ev["cve_ok"] is False:
            # verification failure; group by the CVE/verifier name
            pattern = f"cve:{ev.get('cve', 'unknown')}"
        elif ev.get("verdict") in ("fail", "reject", "blocked"):
            pattern = f"verdict:{ev.get('verdict')}"
        elif ftype in ("task.implemented", "artifact.produced") and ev.get("returncode", 0) not in (0, None):
            pattern = f"returncode:{ev.get('returncode')}"

        if pattern:
            item = dict(ev)
            item["pattern"] = pattern
            if reason:
                item["_reason"] = reason
            failures.append(item)
    return failures


def _group_by_pattern(failures: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in failures:
        groups[f["pattern"]].append(f)
    return groups


def _propose_upgrades(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Build proposals for patterns that crossed the recurrence threshold."""
    proposals: list[dict[str, Any]] = []
    for pattern, items in groups.items():
        if len(items) < THRESHOLD:
            continue
        reasons = [i.get("_reason", "") for i in items if i.get("_reason")]
        common = Counter(reasons).most_common(1)
        sample_reason = common[0][0] if common else "(no reason recorded)"
        target = "acceptance-templates" if pattern.startswith("cve:") else "constitution"
        proposals.append({
            "pattern": pattern,
            "count": len(items),
            "target": target,
            "sample_reason": sample_reason,
            "text": _render_template(pattern, len(items), sample_reason, target),
        })
    return proposals


def _render_template(pattern: str, count: int, reason: str, target: str) -> str:
    kind = "acceptance template" if target == "acceptance-templates" else "constitution clause"
    return (
        f"- [auto] failure pattern `{pattern}` recurred {count}x. "
        f"Suggested {kind}: guard against `{reason}` by adding an explicit "
        f"acceptance verb/args or a constitutional rule."
    )


def mine(dry_run: bool = False) -> dict[str, Any]:
    """Mine the ledger for recurring failures and propose upgrades.

    Returns a dict with ``proposals`` (list) and, when not dry-run, the
    recorded ``event_ids``.
    """
    ledger = Ledger(str(LEDGER_PATH))
    events = ledger.load_flat()
    failures = _extract_failures(events)
    groups = _group_by_pattern(failures)
    proposals = _propose_upgrades(groups)

    result: dict[str, Any] = {
        "events_scanned": len(events),
        "failures_found": len(failures),
        "proposals": proposals,
        "dry_run": dry_run,
    }

    if dry_run or not proposals:
        return result

    # record each proposal as a design.proposed event and append to the target file
    seq = Sequencer(str(LEDGER_PATH))
    seq.start()
    for p in proposals:
        seq.propose("EVOLVE", "design.proposed",
                    pattern=p["pattern"], count=p["count"],
                    target=p["target"], text=p["text"])
        _append_to_target(p["target"], p["text"])
    seq.stop()
    return result


def _append_to_target(target: str, text: str) -> None:
    path = ACCEPTANCE_TEMPLATES_PATH if target == "acceptance-templates" else CONSTITUTION_PATH
    header = "# Acceptance Templates\n\n" if target == "acceptance-templates" else "# Constitution\n\n"
    if not path.exists():
        path.write_text(header, encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def report(result: dict[str, Any]) -> str:
    lines = [
        f"events scanned : {result['events_scanned']}",
        f"failures found : {result['failures_found']}",
        f"proposals      : {len(result['proposals'])}",
        "",
    ]
    if not result["proposals"]:
        lines.append("(no recurring failure pattern reached the threshold of "
                     f"{THRESHOLD} occurrences — nothing to propose)")
    else:
        for i, p in enumerate(result["proposals"], 1):
            lines.append(f"{i}. pattern `{p['pattern']}` x{p['count']} -> {p['target']}")
            lines.append(f"   sample: {p['sample_reason']}")
            lines.append(f"   {p['text']}")
    if result.get("event_ids"):
        lines.append("")
        lines.append(f"recorded design.proposed events: {', '.join(result['event_ids'])}")
    return "\n".join(lines)
