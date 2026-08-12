#!/usr/bin/env python
"""Stage 0 CLI tests: review / log / show drive the pipeline and ledger."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
import os
from pathlib import Path
_cve_candidate = Path(__file__).resolve().parents[2] / ".cve-venv" / "Scripts" / "python.exe"
CVE = os.environ.get(
    "CVE_PYTHON",
    str(_cve_candidate) if _cve_candidate.exists() else sys.executable,
)
CLI = ["-m", "harness.cli"]
CASE = str(REPO / "probe" / "n3" / "caseGreen")


def _run(*cli_args, expect_rc=0, env=None):
    cmd = [CVE, *CLI, *cli_args]
    run_env = {**os.environ, **env} if env else None
    res = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, env=run_env)
    assert res.returncode == expect_rc, f"rc={res.returncode} stderr={res.stderr}"
    return res


def _sample_tasks_md() -> str:
    """A minimal but well-formed tasks.md that parse_tasks_md()/structural_check()
    accept, built via the actual renderer so the guard tests below don't rely
    on hand-guessed Markdown syntax."""
    from harness.roles.decomposer import render_tasks_md
    return render_tasks_md([{
        "task_id": "T1", "goal": "g",
        "acceptance": [{"verb": "pytest", "args": ["tests/"], "expect_exit": 0}],
        "depends_on": [], "touch_allow": ["src/a.py"],
    }], "req")


def test_review_dry_run_writes_pipeline_events(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    # clean ledger so we count this task's events only
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    res = _run("review", CASE, "--reviewer", "codex", "--dry-run")
    j = json.loads(res.stdout)
    # dry_run still runs CVE; verdict reflects reviewer skip
    assert j["tree_hash"], "tree_hash must be bound"
    assert j["verdict"] in ("pass", "fail", "judgment_unavailable", "environment_error")
    # ledger has verification.run for this task (dry-run skips judgment; see
    # test_log_shows_judgment_when_present for the judgment-present path)
    lg = (REPO / "harness" / "ledger" / "events.jsonl").read_text(encoding="utf-8")
    assert "verification.run" in lg


def test_review_task_handoff_resolves_worktree_and_acceptance(tmp_path, monkeypatch):
    """Stage 5 handoff: `review-task --task T1 --task_file dag.md` resolves acceptance
    and the worktree path from the implemented task (no live vendor)."""
    monkeypatch.chdir(REPO)
    # write a task DAG with T1 having a custom acceptance
    dag = tmp_path / "tasks.md"
    dag.write_text(
        "# タスク分解\n要求: demo\nタスク数: 1\n\n"
        "## 1. T1\n\n- 目標: g\n- 依存: （なし）\n"
        "- 触ってよい範囲: wclite/core.py\n"
        "- 受入基準 (1):\n  - `pytest` tests/test_core.py (expect_exit=0)\n",
        encoding="utf-8")
    # stage a worktree dir so target.exists() passes
    wt = REPO / "workspaces" / "T1"
    wt.mkdir(parents=True, exist_ok=True)
    try:
        ledger = REPO / "harness" / "ledger" / "events.jsonl"
        if ledger.exists():
            ledger.unlink()
        res = _run("review-task", "--task", "T1", "--task_file", str(dag),
                   "--reviewer", "codex", "--dry-run")
        j = json.loads(res.stdout)
        assert j["tree_hash"], "tree_hash must be bound (CVE ran)"
        assert j["verdict"] in ("pass", "fail", "judgment_unavailable", "environment_error")
        # the task id is reused (T1), not a fresh T-xxxx
        lg = ledger.read_text(encoding="utf-8")
        assert "T1:" in lg
        assert "verification.run" in lg
    finally:
        import shutil
        shutil.rmtree(wt, ignore_errors=True)
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    if ledger.exists():
        ledger.unlink()
    res = _run("review", CASE, "--reviewer", "codex", "--dry-run")
    # recover the task id from the ledger
    # recover the task id from the ledger (chunk layout: first chunk's first event)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    first_id = json.loads(lines[0])["events"][0]["event_id"].split(":")[0]
    out = _run("log", first_id).stdout
    assert "verification.run" in out
    # judgment is not emitted by dry-run review; covered by
    # test_log_shows_judgment_when_present (assumed/returned judgment path)


def test_status_runs(monkeypatch):
    monkeypatch.chdir(REPO)
    out = _run("status").stdout
    assert "events in ledger" in out


def test_drive_speculative_flag_is_accepted(monkeypatch):
    """The --speculative flag must be accepted by the CLI parser and forwarded
    to drive() (no error). Use --dry-run so no live vendor is invoked."""
    monkeypatch.chdir(REPO)
    spec = REPO / "probe" / "sample" / "my-design.md"
    dag = REPO / "probe" / "sample" / "my-design-tasks-parallel.md"
    res = _run("drive", "--design_file", str(spec), "--task_file", str(dag), "--speculative", "--dry-run")
    # dry-run drive prints a JSON summary with ok=True
    import json as _json
    j = _json.loads(res.stdout)
    assert j["ok"] is True


def test_drive_default_has_no_speculative_flag_in_help(monkeypatch):
    """Sanity: --speculative appears in drive's help (so it is wired up)."""
    monkeypatch.chdir(REPO)
    res = _run("drive", "--help")
    assert "--speculative" in res.stdout


def test_cli_dashboard_md_stdout(monkeypatch):
    monkeypatch.chdir(REPO)
    res = _run("dashboard", "--format", "md")
    assert res.returncode == 0
    assert "Dashboard" in res.stdout or "Task" in res.stdout or "#" in res.stdout


def test_cli_dashboard_both_writes_two_files(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    out_dir = tmp_path / "dash_out"
    res = _run("dashboard", "--format", "both", "--out", str(out_dir))
    assert res.returncode == 0
    md_file = out_dir / "dashboard.md"
    html_file = out_dir / "dashboard.html"
    assert md_file.exists()
    assert html_file.exists()
    assert len(md_file.read_text(encoding="utf-8")) > 0
    assert len(html_file.read_text(encoding="utf-8")) > 0


def test_cli_dashboard_uses_role_renderers(tmp_path, monkeypatch):
    """Regression: cmd_dashboard must import build_model/render_markdown/
    render_html from harness.roles.dashboard (single source of truth) rather
    than falling back to an inline duplicate definition in cli.py.
    We verify by feeding a synthetic ledger and checking the rendered output
    reflects the role module's canonical status mapping + markdown shape."""
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    had = ledger.exists()
    backup = ledger.read_text(encoding="utf-8") if had else None
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "\n".join([
            '{"design_file":"design.md","task_file":"tasks.md","events":['
            '{"event_id":"T1:1","type":"task.created"},'
            '{"event_id":"T1:5","type":"integrate.ok"},'
            '{"event_id":"T2:2","type":"implementer.error","error":"boom"}'
            ']}',
        ]) + "\n",
        encoding="utf-8",
    )
    try:
        res = _run("dashboard", "--format", "md")
        assert res.returncode == 0
        out = res.stdout
        # role module keys by task_id (not event_id) and maps statuses
        assert "| T1 | integrated |" in out
        assert "| T2 | failed |" in out
        # role module's markdown header
        assert out.startswith("# Dashboard")
    finally:
        if backup is not None:
            ledger.write_text(backup, encoding="utf-8")
        elif had:
            ledger.unlink()


def test_cli_dashboard_watch_regenerates_until_interrupted(tmp_path, monkeypatch):
    """--watch loops _render_dashboard_once()/time.sleep(interval) until
    Ctrl+C (docs/design/timeout-liveness-watchdog.md §5). Tested in-process
    (not via the subprocess-based _run helper) since a real infinite loop
    can't be driven through a spawned CLI process in a unit test; time.sleep
    is monkeypatched to raise KeyboardInterrupt so the loop runs exactly one
    iteration."""
    import argparse
    import harness.cli as cli_mod

    ledger = tmp_path / "events.jsonl"
    out_file = tmp_path / "dashboard.html"
    monkeypatch.setattr(cli_mod, "LEDGER_PATH", ledger)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)

    ns = argparse.Namespace(format="html", out=str(out_file), watch=True, interval=7)
    rc = cli_mod.cmd_dashboard(ns)

    assert rc == 0
    assert sleep_calls == [7]
    assert out_file.exists()
    assert '<meta http-equiv="refresh" content="7">' in out_file.read_text(encoding="utf-8")


def test_cli_dashboard_watch_defaults_interval_when_omitted(tmp_path, monkeypatch):
    import argparse
    import harness.cli as cli_mod

    ledger = tmp_path / "events.jsonl"
    monkeypatch.setattr(cli_mod, "LEDGER_PATH", ledger)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)

    ns = argparse.Namespace(format="md", out=None, watch=True, interval=None)
    rc = cli_mod.cmd_dashboard(ns)

    assert rc == 0
    assert sleep_calls == [cli_mod._DEFAULT_WATCH_INTERVAL]


def test_cli_dashboard_without_watch_does_not_loop(tmp_path, monkeypatch, capsys):
    import argparse
    import harness.cli as cli_mod

    ledger = tmp_path / "events.jsonl"
    monkeypatch.setattr(cli_mod, "LEDGER_PATH", ledger)

    def fail_sleep(seconds):
        raise AssertionError("non-watch mode must not sleep/loop")

    monkeypatch.setattr(cli_mod.time, "sleep", fail_sleep)

    ns = argparse.Namespace(format="md", out=None, watch=False, interval=None)
    rc = cli_mod.cmd_dashboard(ns)
    assert rc == 0


def test_architect_recovers_requirement_from_first_line(tmp_path, monkeypatch):
    """`architect --design_file <existing file>` with requirement omitted recovers it
    from the file's first line (stripping a leading '# 設計:' marker).
    Uses an isolated ledger (SUPER_AGENT_LEDGER) rather than the shared
    real ledger, which is prone to Windows PermissionError when open elsewhere."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    spec = tmp_path / "header-design.md"
    spec.write_text("# 設計: サンプルWeb API\n\n## エンドポイント\nGET /health\n", encoding="utf-8")
    _run("architect", "--design_file", str(spec), "--dry-run", env={"SUPER_AGENT_LEDGER": str(ledger)})
    lg = ledger.read_text(encoding="utf-8")
    assert '"goal": "サンプルWeb API"' in lg or '"goal":"サンプルWeb API"' in lg


def test_architect_recovers_requirement_from_plain_first_line(tmp_path, monkeypatch):
    """Files without the '# 設計:' marker recover requirement, write a completed design
    file separately, and register the new path in the ledger."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    spec = tmp_path / "plain-design.md"
    original_text = "素のテキストの1行目\n\n本文\n"
    spec.write_text(original_text, encoding="utf-8")

    res = _run("architect", "--design_file", str(spec), "--dry-run", env={"SUPER_AGENT_LEDGER": str(ledger)})
    lg = ledger.read_text(encoding="utf-8")

    # Original file must remain untouched
    assert spec.read_text(encoding="utf-8") == original_text

    # Ledger goal recovered
    assert '"goal": "素のテキストの1行目"' in lg or '"goal":"素のテキストの1行目"' in lg

    # Ledger design_file must point to the new completed file path under design_dir (not original spec)
    assert str(spec) not in lg
    assert "docs" in lg and "design" in lg


def test_architect_requires_requirement_when_spec_missing(tmp_path, monkeypatch):
    """Without --design_file (or a non-existent --design_file target), requirement is still
    mandatory: the CLI must fail fast rather than silently proceeding empty."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    res = _run("architect", "--dry-run", expect_rc=1, env={"SUPER_AGENT_LEDGER": str(ledger)})
    j = json.loads(res.stdout)
    assert j["ok"] is False
    assert "requirement" in j["error"]


def test_log_shows_judgment_when_present(tmp_path, monkeypatch):
    """When a judgment event IS present in the ledger (assumed/returned), `log`
    must surface it. We inject a judgment event via a fixture chunk rather than
    relying on the live review dry-run (which skips judgment because cve_ok=True).
    """
    monkeypatch.chdir(REPO)
    ledger = REPO / "harness" / "ledger" / "events.jsonl"
    had = ledger.exists()
    backup = ledger.read_text(encoding="utf-8") if had else None
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # chunk with a task that has verification.run + judgment (assumed returned)
    ledger.write_text(
        '{"design_file":"design.md","task_file":"tasks.md","events":['
        '{"event_id":"TJ:1","type":"task.created"},'
        '{"event_id":"TJ:2","type":"verification.run","tree_hash":"abc"},'
        '{"event_id":"TJ:3","type":"judgment","verdict":"PASS","tree_hash":"abc"}'
        ']}\n',
        encoding="utf-8",
    )
    try:
        out = _run("log", "TJ").stdout
        assert "verification.run" in out
        assert "judgment" in out
    finally:
        if backup is not None:
            ledger.write_text(backup, encoding="utf-8")
        elif had:
            ledger.unlink()


def test_plan_guard_a_rejects_existing_auto_named_tasks_file(tmp_path, monkeypatch):
    """`plan --design_file X "<req>"` (no --task_file): if the auto-named task path
    already has a file, `plan` must fail instead of silently reusing it or
    picking a `-2` suffix (task files no longer use unique_path's
    collision-avoiding numbering)."""
    from harness.core.invoke import default_task_path, slugify

    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    spec = tmp_path / "design.md"
    spec.write_text("# 設計: サンプル\n", encoding="utf-8")
    requirement = "Add login feature"
    collide = default_task_path(str(spec), slugify(requirement))
    collide.parent.mkdir(parents=True)
    collide.write_text("# already here\n", encoding="utf-8")

    res = _run("plan", requirement, "--design_file", str(spec), "--dry-run",
              expect_rc=1, env={"SUPER_AGENT_LEDGER": str(ledger)})
    j = json.loads(res.stdout)
    assert j["ok"] is False
    assert "already exists" in j["error"]


def test_plan_guard_b_rejects_task_file_registered_under_other_design(tmp_path, monkeypatch):
    """`plan --design_file Y --task_file T`: if the ledger already has T registered under
    a DIFFERENT design_file X, this must fail (reusing a task file across two
    designs would silently merge unrelated task DAGs)."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    design_x = tmp_path / "x.md"
    design_x.write_text("# 設計: X\n", encoding="utf-8")
    design_y = tmp_path / "y.md"
    design_y.write_text("# 設計: Y\n", encoding="utf-8")
    tasks_t = tmp_path / "t.md"
    tasks_t.write_text(_sample_tasks_md(), encoding="utf-8")

    # seed the ledger: T is already registered under design_x
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"design_file": str(design_x.resolve()),
                   "task_file": str(tasks_t.resolve()),
                   "events": [{"event_id": "T-seed:1", "type": "task.created"}]},
                  ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    res = _run("plan", "--design_file", str(design_y), "--task_file", str(tasks_t), "--dry-run",
              expect_rc=1, env={"SUPER_AGENT_LEDGER": str(ledger)})
    j = json.loads(res.stdout)
    assert j["ok"] is False
    assert "design_file" in j["error"]


def test_plan_guard_b_allows_reuse_under_same_design(tmp_path, monkeypatch):
    """Same setup, but --design_file matches the design_file already recorded for T:
    this must still succeed and reuse the tasks file (no vendor call)."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    design_x = tmp_path / "x.md"
    design_x.write_text("# 設計: X\n", encoding="utf-8")
    tasks_t = tmp_path / "t.md"
    tasks_t.write_text(_sample_tasks_md(), encoding="utf-8")

    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"design_file": str(design_x.resolve()),
                   "task_file": str(tasks_t.resolve()),
                   "events": [{"event_id": "T-seed:1", "type": "task.created"}]},
                  ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    res = _run("plan", "--design_file", str(design_x), "--task_file", str(tasks_t), "--dry-run",
              env={"SUPER_AGENT_LEDGER": str(ledger)})
    out = json.loads(res.stdout)
    assert out["decompose"].get("reused_tasks_file") is True
    assert out["schedule"]["ok"] is True


def test_plan_resolves_spec_from_ledger_via_tasks(tmp_path, monkeypatch):
    """`plan --task_file T` (no --design_file): if T is already registered in the
    ledger, --design_file is auto-resolved to its recorded design_file instead of
    erroring out."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    design_x = tmp_path / "x.md"
    design_x.write_text("# 設計: X\n", encoding="utf-8")
    tasks_t = tmp_path / "t.md"
    tasks_t.write_text(_sample_tasks_md(), encoding="utf-8")

    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"design_file": str(design_x.resolve()),
                   "task_file": str(tasks_t.resolve()),
                   "events": [{"event_id": "T-seed:1", "type": "task.created"}]},
                  ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    res = _run("plan", "--task_file", str(tasks_t), "--dry-run",
              env={"SUPER_AGENT_LEDGER": str(ledger)})
    out = json.loads(res.stdout)
    assert out["decompose"].get("reused_tasks_file") is True


def test_plan_tasks_only_unregistered_still_errors(tmp_path, monkeypatch):
    """`plan --task_file T` where T is a real file but NOT registered in the
    ledger under any design: resolve_spec() can't recover a design_file, so
    this must still fail (a truly first-time file, not a reuse)."""
    monkeypatch.chdir(REPO)
    ledger = tmp_path / "events.jsonl"
    tasks_t = tmp_path / "t.md"
    tasks_t.write_text(_sample_tasks_md(), encoding="utf-8")
    res = _run("plan", "--task_file", str(tasks_t), "--dry-run",
              expect_rc=1, env={"SUPER_AGENT_LEDGER": str(ledger)})
    j = json.loads(res.stdout)
    assert j["ok"] is False
    assert "cannot determine design_file" in j["error"]


def test_resolve_design_file_arg_infers_from_directory_structure(tmp_path):
    from harness.cli import resolve_design_file_arg, argparse
    from harness.core.ledger import Sequencer
    design_file = tmp_path / "docs" / "design" / "my_feature.md"
    design_file.parent.mkdir(parents=True, exist_ok=True)
    design_file.write_text("# 設計: My Feature\n\nSome spec", encoding="utf-8")

    task_file = tmp_path / "docs" / "design" / "my_feature_tasks" / "drive.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text("# tasks", encoding="utf-8")

    seq = Sequencer(tmp_path / "events.jsonl")
    args = argparse.Namespace(design_file=None, task_file=str(task_file), requirement=None)
    resolved = resolve_design_file_arg(args, seq)
    assert resolved == str(design_file)


