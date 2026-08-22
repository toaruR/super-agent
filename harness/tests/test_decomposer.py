#!/usr/bin/env python
"""Stage 2 (decomposer) tests: structural contract + CLI dry-run."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
import os
from pathlib import Path
CVE = os.environ.get(
    "CVE_PYTHON",
    str(Path(__file__).resolve().parents[2] / ".cve-venv" / "Scripts" / "python.exe"),
)
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


# ---- test-file protection (implementer must not be able to rewrite the very
# test it's graded by; see this session's design discussion on self-scoring) ----
def test_structural_check_allows_touch_allow_on_own_test_file():
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [
        {"task_id": "T1", "goal": "g",
         "acceptance": [{"verb": "pytest", "args": ["tests/test_core.py", "-k", "test_x"]}],
         "touch_allow": ["src/a.py", "tests/test_core.py"]},
    ]
    errs = structural_check(tasks, reg)
    assert errs == []


def test_structural_check_allows_lint_verb_on_touch_allow_file():
    """mypy/ruff args name the *implementation* file under check, which
    legitimately belongs in touch_allow — must not be flagged as protection."""
    from harness.roles.decomposer import structural_check, VerifierRegistry
    reg = VerifierRegistry(REPO / "harness" / "config" / "verifiers.yaml")
    tasks = [
        {"task_id": "T1", "goal": "g",
         "acceptance": [{"verb": "mypy", "args": ["src/a.py"]}],
         "touch_allow": ["src/a.py"]},
    ]
    assert structural_check(tasks, reg) == []


# ---- decompose() design_file propagation (regression: CLAUDE.md「decompose()
# だけが design_file を受け取れずチャンクが分裂する」バグ) ----
class _StubSeq:
    """Minimal seq stand-in: records every propose() call's kwargs."""
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.path = "docs/design/_stub_ledger.jsonl"

    def propose(self, task_id, type_, **fields):
        self.calls.append((task_id, type_, fields))


def test_decompose_propagates_design_file_to_all_events(monkeypatch):
    """decompose(design_file=...) must tag every ledger event it proposes
    with that design_file (not just some), so they all land in the same
    (design_file, task_file) chunk instead of splintering off into a
    design_file="" chunk."""
    from harness.roles import decomposer

    fake_result = {
        "result": {
            "tasks": [{
                "task_id": "T1", "goal": "g",
                "acceptance": [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}],
                "depends_on": [], "touch_allow": ["src/a.py"],
            }]
        }
    }
    monkeypatch.setattr(decomposer, "invoke", lambda *a, **k: fake_result)
    monkeypatch.setattr(decomposer, "write_progress", lambda *a, **k: None)

    seq = _StubSeq()
    out = decomposer.decompose("T-parent", "req", vendor="claude", seq=seq,
                               design_file="docs/design/x.md")
    assert out["ok"] is True
    assert seq.calls, "expected at least one propose() call"
    for _tid, _typ, fields in seq.calls:
        assert fields.get("design_file") == "docs/design/x.md"
    types = [t for _, t, _ in seq.calls]
    assert "decompose.ok" in types
    assert "task.created" in types


def test_decompose_propagates_design_file_on_rejection(monkeypatch):
    """Same guarantee on the structural-check-failed path (decompose.rejected)."""
    from harness.roles import decomposer

    fake_result = {"result": {"tasks": [{"task_id": "T1", "goal": "g", "acceptance": []}]}}
    monkeypatch.setattr(decomposer, "invoke", lambda *a, **k: fake_result)
    monkeypatch.setattr(decomposer, "write_progress", lambda *a, **k: None)

    seq = _StubSeq()
    out = decomposer.decompose("T-parent", "req", vendor="claude", seq=seq,
                               design_file="docs/design/y.md")
    assert out["ok"] is False
    assert seq.calls and seq.calls[0][1] == "decompose.rejected"
    assert seq.calls[0][2].get("design_file") == "docs/design/y.md"


# ---- CLI dry-run (no vendor call) ----
def test_plan_dry_run_assembles_prompt(monkeypatch, tmp_path):
    monkeypatch.chdir(REPO)
    # --design_file is now required (either explicit or resolved from the ledger via
    # an existing --task_file); pass an explicit design file here.
    spec = tmp_path / "design.md"
    spec.write_text("# 設計: Web API を作れ\n", encoding="utf-8")
    res = _run("plan", "Web API を作れ", "--design_file", str(spec), "--dry-run")
    out = json.loads(res.stdout)
    assert out["decompose"]["dry_run"] is True
    assert "cmd" in out["decompose"]
    assert "schedule" in out


def test_plan_spec_consumes_architect_output(monkeypatch, tmp_path):
    monkeypatch.chdir(REPO)
    # design file as produced by `architect` (has '# 設計:' header)
    design = "# 設計: Excel等からオントロジーを作りたい\n\n## 入力アダプタ\n...\n"
    spec = tmp_path / "my-design.md"
    spec.write_text(design, encoding="utf-8")
    res = _run("plan", "--design_file", str(spec), "--dry-run")
    out = json.loads(res.stdout)
    assert out["decompose"]["dry_run"] is True
    # requirement recovered from the design header -> prompt assembled without error
    assert "cmd" in out["decompose"]


def test_plan_no_requirement_no_spec_errors(monkeypatch):
    """`plan --dry-run` with neither a requirement nor --design_file/--task_file given
    can't determine a design_file, so it fails fast (§5 resolve_spec())."""
    monkeypatch.chdir(REPO)
    res = subprocess.run([CVE, "-m", "harness.cli", "plan", "--dry-run"],
                         cwd=str(REPO), capture_output=True, text=True)
    assert res.returncode == 1
    assert "cannot determine design_file" in res.stdout


def test_decompose_out_writes_markdown(monkeypatch, tmp_path):
    """--task_file writes the decomposed DAG as Markdown (no vendor call: dry-run can't,
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


def test_parse_tasks_md_roundtrip(monkeypatch, tmp_path):
    """parse_tasks_md reverses render_tasks_md so `plan --task_file <existing>`
    can reuse a hand-edited file without calling the vendor."""
    from harness.roles.decomposer import render_tasks_md, parse_tasks_md
    tasks = [
        {"task_id": "T1", "goal": "core を実装",
         "acceptance": [{"verb": "pytest", "args": ["tests/test_core.py"], "expect_exit": 0}],
         "depends_on": [], "touch_allow": ["wclite/core.py"],
         "rubric": [{"criterion": "edge case handled", "weight": 60},
                    {"criterion": "test not modified", "weight": 40}],
         "rubric_threshold": 75},
        {"task_id": "T2", "goal": "cli を実装",
         "acceptance": [{"verb": "pytest", "args": ["tests/test_cli.py"], "expect_exit": 0}],
         "depends_on": ["T1"], "touch_allow": ["wclite/cli.py"]},
    ]
    md = render_tasks_md(tasks, "wc-lite ツール")
    p = tmp_path / "tasks.md"
    p.write_text(md, encoding="utf-8")
    back = parse_tasks_md(str(p))
    assert len(back) == 2
    assert back[0]["task_id"] == "T1"
    assert back[0]["goal"] == "core を実装"
    assert back[0]["depends_on"] == []
    assert back[1]["depends_on"] == ["T1"]
    assert back[0]["acceptance"][0]["verb"] == "pytest"
    assert back[0]["acceptance"][0]["args"] == ["tests/test_core.py"]
    assert back[0]["rubric_threshold"] == 75
    assert back[0]["rubric"] == [{"criterion": "edge case handled", "weight": 60},
                                  {"criterion": "test not modified", "weight": 40}]
    assert back[1]["rubric"] == []


def test_plan_reuses_existing_tasks_file(monkeypatch, tmp_path):
    """`plan --design_file X --task_file <existing.md>` skips the vendor and schedules
    directly (guard B allows it: T is not yet registered in the ledger under
    any other design_file, so this first use is fine)."""
    from harness.roles.decomposer import render_tasks_md, parse_tasks_md
    tasks = [{
        "task_id": "T1", "goal": "g",
        "acceptance": [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}],
        "depends_on": [], "touch_allow": ["src/a.py"],
    }]
    md = tmp_path / "tasks.md"
    md.write_text(render_tasks_md(tasks, "req"), encoding="utf-8")
    spec = tmp_path / "design.md"
    spec.write_text("# 設計: req\n", encoding="utf-8")
    monkeypatch.chdir(REPO)
    res = _run("plan", "--design_file", str(spec), "--task_file", str(md), "--dry-run")
    out = json.loads(res.stdout)
    # reused_tasks_file marks the no-vendor path
    assert out["decompose"].get("reused_tasks_file") is True
    assert out["schedule"]["ok"] is True


def test_decomposer_draft_saved_and_resumed(tmp_path, monkeypatch):
    """decomposer 実行時に途中失敗で .draft が作成され、再実行時にプロンプトへ引き継がれ、
    成功時に .draft が削除されることを確認する。"""
    import subprocess as _sp
    from harness.core.invoke import atomic_write_draft
    import harness.roles.decomposer as decomp_mod

    task_file = tmp_path / "tasks.md"
    draft_path = tmp_path / "tasks.md.draft"

    prompts_captured = []

    def fake_invoke_fail(decl, prompt, **kw):
        prompts_captured.append(prompt)
        dp = kw.get("draft_path")
        if dp:
            atomic_write_draft(dp, "途中の思考ログ: タスク分解案")
        raise _sp.TimeoutExpired(cmd=[decl.name], timeout=1800)

    def fake_invoke_pass(decl, prompt, **kw):
        prompts_captured.append(prompt)
        return {"cmd": [decl.name], "returncode": 0,
                "result": {"tasks": [{"task_id": "T1", "goal": "goal1",
                                       "acceptance": [{"verb": "pytest", "args": ["tests/"]}]}]}}

    # 1. First run: timeout failure, leaves .draft behind
    monkeypatch.setattr(decomp_mod, "invoke", fake_invoke_fail)
    res1 = decomp_mod.decompose("T1", "要件", "claude", task_file=str(task_file))
    assert res1.get("ok") is False or "error" in res1
    assert draft_path.exists()
    assert "途中の思考ログ" in draft_path.read_text(encoding="utf-8")

    # 2. Second run: recovers .draft into prompt and succeeds
    monkeypatch.setattr(decomp_mod, "invoke", fake_invoke_pass)
    res2 = decomp_mod.decompose("T2", "要件", "agy", task_file=str(task_file))
    assert res2.get("ok") is True
    assert not draft_path.exists()  # draft deleted after success
    assert "前回の試行で途中まで作成されたタスク分解ドラフト" in prompts_captured[-1]
    assert "途中の思考ログ: タスク分解案" in prompts_captured[-1]

