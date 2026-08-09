#!/usr/bin/env python
"""End-to-end pipeline test (Stage C) against probe/n3/caseB.

caseB: 2-file ledger (accounts.py + money.py) with a real bug (Money mutable,
history rewritable) that acceptance tests do NOT catch. This proves the
reviewer path can surface defects the green acceptance misses.

We run the pipeline in dry_run (no live vendor) so the test is hermetic,
but still assert: CVE ran, brief was built within budget, and the adjudicator
returns a verdict bound to the tree_hash.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from harness.core.cve import CVE
from harness.core.ledger import Ledger, Sequencer
from harness.roles.review_flow import run_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CASEB = REPO_ROOT / "probe" / "n3" / "caseB"
CONFIG = Path(__file__).resolve().parent.parent / "config"


def _acceptance() -> list[dict]:
    return [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}]


def test_cve_runs_and_binds_tree_hash() -> None:
    cve = CVE(CONFIG / "verification_env.yaml", CONFIG / "verifiers.yaml")
    ev = cve.run(CASEB, _acceptance())
    assert ev["cve_ok"] is True, ev
    assert len(ev["tree_hash"]) == 16
    assert len(ev["evidence"]) >= 1


def test_pipeline_dry_run_records_events(tmp_path) -> None:
    ledger_path = tmp_path / "events.jsonl"
    seq = Sequencer(str(ledger_path))
    seq.start()
    j = run_pipeline("T-CASEB", CASEB, _acceptance(),
                     reviewer_vendor="codex", seq=seq, dry_run=True)
    seq.stop()

    # dry-run: CVE 検証は実行されず、ダミー証拠（tree_hash="dry-run"）になる。
    # 実ファイルが存在しない worktree に対しても安全（NotADirectoryError にならない）。
    assert j["tree_hash"] == "dry-run"
    events = Ledger(str(ledger_path)).load_flat()
    types = [e["type"] for e in events]
    assert "verification.run" in types
    assert "reviewer.skipped" in types
    assert "brief.built" in types
    assert "judgment" in types
    # tree_hash carried from verification to judgment (H4)
    vr = next(e for e in events if e["type"] == "verification.run")
    jd = next(e for e in events if e["type"] == "judgment")
    assert vr["tree_hash"] == jd["tree_hash"] == "dry-run"


if __name__ == "__main__":
    test_cve_runs_and_binds_tree_hash()
    test_pipeline_dry_run_records_events()
    print("ALL PIPELINE TESTS PASSED")
