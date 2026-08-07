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


def test_decompose_spec_consumes_architect_output(monkeypatch, tmp_path):
    monkeypatch.chdir(REPO)
    # design file as produced by `architect` (has '# 設計:' header)
    design = "# 設計: Excel等からオントロジーを作りたい\n\n## 入力アダプタ\n...\n"
    spec = tmp_path / "my-design.md"
    spec.write_text(design, encoding="utf-8")
    res = _run("decompose", "--spec", str(spec), "--dry-run")
    out = json.loads(res.stdout)
    assert out["dry_run"] is True
    # requirement recovered from the design header -> prompt assembled without error
    assert "cmd" in out


def test_decompose_no_requirement_no_spec_errors(monkeypatch):
    monkeypatch.chdir(REPO)
    res = subprocess.run([CVE, "-m", "harness.cli", "decompose", "--dry-run"],
                         cwd=str(REPO), capture_output=True, text=True)
    assert res.returncode == 1
    assert "requirement or --spec is required" in res.stdout


def test_decompose_out_writes_markdown(monkeypatch, tmp_path):
    """--out writes the decomposed DAG as Markdown (no vendor call: dry-run can't,
    so test the renderer directly)."""
    from harness.roles.decomposer import render_tasks_md
    tasks = [{
        "task_id": "T1", "goal": "g",
        "acceptance": [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}],
        "depends_on": [], "touch_allow": ["src/a.py"],
    }]
    md = render_tasks_md(tasks, "demo requirement")
    assert "demo requirement" in md
    assert "T1" in md
    assert "`pytest` tests/" in md
    out = tmp_path / "tasks.md"
    out.write_text(md, encoding="utf-8")
    assert out.exists()
