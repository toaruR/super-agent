"""Tests for the Stage 6 self-improvement (evolve) role."""

import json
from pathlib import Path

from harness.roles import improver


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def test_extract_failures_detects_cve_failure():
    evs = [
        {"event_id": "X:1", "task_id": "X", "seq": 1,
         "type": "verification.run", "cve": "pytest", "cve_ok": False},
        {"event_id": "X:2", "task_id": "X", "seq": 2,
         "type": "verification.run", "cve": "pytest", "cve_ok": True},
    ]
    fails = improver._extract_failures(evs)
    assert len(fails) == 1
    assert fails[0]["pattern"] == "cve:pytest"


def test_group_and_propose_threshold():
    evs = [
        {"event_id": f"X:{i}", "task_id": "X", "seq": i,
         "type": "verification.run", "cve": "flake", "cve_ok": False,
         "reason": "flaky import"}
        for i in range(3)
    ]
    groups = improver._group_by_pattern(improver._extract_failures(evs))
    proposals = improver._propose_upgrades(groups)
    assert len(proposals) == 1
    assert proposals[0]["pattern"] == "cve:flake"
    assert proposals[0]["count"] == 3
    assert proposals[0]["target"] == "acceptance-templates"


def test_below_threshold_no_proposal():
    evs = [
        {"event_id": f"X:{i}", "task_id": "X", "seq": i,
         "type": "verification.run", "cve": "flake", "cve_ok": False}
        for i in range(2)  # fewer than THRESHOLD
    ]
    groups = improver._group_by_pattern(improver._extract_failures(evs))
    assert improver._propose_upgrades(groups) == []


def test_mine_dry_run_does_not_write_ledger(tmp_path):
    ledger_path = tmp_path / "events.jsonl"
    _write_events(ledger_path, [
        {"event_id": f"X:{i}", "task_id": "X", "seq": i,
         "type": "verification.run", "cve": "flake", "cve_ok": False}
        for i in range(3)
    ])
    # point module globals at the temp ledger
    improver.LEDGER_PATH = ledger_path
    result = improver.mine(dry_run=True)
    assert result["proposals"], "expected a proposal"
    # dry-run must NOT append to the ledger
    after = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(after) == 3, "dry-run should not add ledger events"


def test_mine_records_design_proposed(tmp_path):
    ledger_path = tmp_path / "events.jsonl"
    _write_events(ledger_path, [
        {"event_id": f"X:{i}", "task_id": "X", "seq": i,
         "type": "verification.run", "cve": "flake", "cve_ok": False}
        for i in range(3)
    ])
    improver.LEDGER_PATH = ledger_path
    # keep the proposal-writing side-effect off the real repo
    improver.CONSTITUTION_PATH = tmp_path / "constitution.md"
    improver.ACCEPTANCE_TEMPLATES_PATH = tmp_path / "acceptance-templates.md"
    result = improver.mine(dry_run=False)
    assert result["proposals"]
    # a design.proposed event should now be in the ledger
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(l).get("type") for l in lines]
    assert "design.proposed" in types
    # and the proposal was written to the (temp) target file
    assert (tmp_path / "acceptance-templates.md").exists()
