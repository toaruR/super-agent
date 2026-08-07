#!/usr/bin/env python
"""Stage 2 (decomposer) tests: structural contract + CLI dry-run."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CVE = r"D:/vagrant/harnesses/super-agent/.cve-venv/Scripts/python.exe"
CLI = ["-m", "harness.cli"]


def _run(*cli_args, expect_rc=0):
    cmd = [CVE, *CLI, *cli_args]
    res = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    assert res.returncode == expect_rc, f"rc={res.returncode} stderr={res.stderr}"
    return res


# ---- structural_check unit tests (no network) ----
def test_structural_check_acceptance_empty():
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [{"task_id": "T1", "goal": "g", "acceptance": []}]
    errs = structural_check(tasks, reg)
    assert any("acceptance が空" in e for e in errs)


def test_structural_check_bad_verb():
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [{"task_id": "T1", "goal": "g",
              "acceptance": [{"verb": "rm", "args": ["-rf", "/"]}]}]
    errs = structural_check(tasks, reg)
    assert any("未登録" in e for e in errs)  # H2: injection verb rejected


def test_structural_check_dag_cycle():
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [
        {"task_id": "T1", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "depends_on": ["T2"]},
        {"task_id": "T2", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "depends_on": ["T1"]},
    ]
    errs = structural_check(tasks, reg)
    assert any("循環" in e for e in errs)


def test_structural_check_touch_overlap():
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [
        {"task_id": "T1", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "touch_allow": ["src/a.py"]},
        {"task_id": "T2", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "touch_allow": ["src/a.py"]},
    ]
    errs = structural_check(tasks, reg)
    assert any("touch_allow 重複" in e for e in errs)


def test_structural_check_ok():
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [
        {"task_id": "T1", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "touch_allow": ["src/a.py"]},
        {"task_id": "T2", "goal": "g", "acceptance": [{"verb": "pytest", "args": ["tests/"]}],
         "depends_on": ["T1"], "touch_allow": ["src/b.py"]},
    ]
    assert structural_check(tasks, reg) == []


# ---- CLI dry-run (no vendor call) ----
def test_decompose_dry_run_assembles_prompt(monkeypatch):
    monkeypatch.chdir(REPO)
    res = _run("decompose", "Web API を作れ", "--dry-run")
    out = json.loads(res.stdout)
    assert out["dry_run"] is True
    assert "cmd" in out


def test_decompose_records_task_created_on_ok(monkeypatch):
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    # dry-run path writes task.created (decomposer role) but not DAG (no LLM)
    res = _run("decompose", "demo", "--dry-run")
    assert res.returncode == 0
    lg = ledger.read_text(encoding="utf-8")
    assert "role\":\"decomposer" in lg or "decomposer" in lg
