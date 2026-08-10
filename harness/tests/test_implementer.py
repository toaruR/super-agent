#!/usr/bin/env python
"""Stage 4 (implementer) tests: prompt assembly, commit binding, ledger events."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _task():
    return {
        "task_id": "T1",
        "goal": "count_words を実装する",
        "acceptance": [{"verb": "pytest", "args": ["tests/test_core.py"], "expect_exit": 0}],
        "depends_on": [],
        "touch_allow": ["wclite/core.py"],
    }


def test_implement_dry_run_assembles_prompt(monkeypatch):
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    out = implement("T1", _task(), "workspaces/T1", vendor="claude", dry_run=True)
    assert out["dry_run"] is True
    assert "cmd" in out
    assert any("count_words" in c for c in out["cmd"])


def test_implement_prompt_includes_rubric_when_present(monkeypatch):
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    task = _task()
    task["rubric"] = [{"criterion": "エッジケースを考慮している", "weight": 60},
                       {"criterion": "テストを書き換えていない", "weight": 40}]
    task["rubric_threshold"] = 75
    out = implement("T1", task, "workspaces/T1", vendor="claude", dry_run=True)
    joined = " ".join(out["cmd"])
    assert "エッジケースを考慮している" in joined
    assert "self_score" in joined


def test_implement_prompt_omits_rubric_section_when_absent(monkeypatch):
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    out = implement("T1", _task(), "workspaces/T1", vendor="claude", dry_run=True)
    joined = " ".join(out["cmd"])
    assert "self_score" not in joined


def test_implement_commits_worktree(monkeypatch, tmp_path):
    """simulate: vendor runs (no-op), then harness commits the allow-listed file."""
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    wt = tmp_path / "wt"
    wt.mkdir()
    # seed a git repo in the worktree
    def git(*a, **k):
        return subprocess.run(["git", "-C", str(wt), *a],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", shell=False)
    git("init", "-q")
    git("config", "user.email", "t@e.st")
    git("config", "user.name", "t")
    git("commit", "--allow-empty", "-m", "init")
    (wt / "wclite").mkdir()
    (wt / "wclite" / "core.py").write_text("def count_words(s): return 0\n",
                                            encoding="utf-8")

    # simulate the vendor having written the implementation (the harness commits)
    (wt / "wclite" / "core.py").write_text(
        "def count_words(s):\n    return len(s.split())\n", encoding="utf-8")

    calls = {"vendor": 0}
    orig_run = subprocess.run

    VENDORS = ("claude", "codex", "agy")

    def fake_run(cmd, *a, **k):
        # no-op the vendor call (don't run the real LLM CLI)
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith(VENDORS):
            calls["vendor"] += 1
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return orig_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = implement("T1", _task(), str(wt), vendor="claude")
    assert out["ok"] is True
    assert out["commit"] is not None
    assert out["tree_hash"] is not None
    assert calls["vendor"] == 1


def test_implement_records_artifact_and_implemented(monkeypatch, tmp_path):
    import subprocess as _sp
    from harness.core.ledger import Ledger, Sequencer
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "wclite").mkdir()
    (wt / "wclite" / "core.py").write_text("x=1\n", encoding="utf-8")
    # simulate the vendor having written the implementation
    (wt / "wclite" / "core.py").write_text("x = 1  # implemented\n", encoding="utf-8")
    # init a git repo in the worktree so the harness can commit
    subprocess.run(["git", "-C", str(wt), "init", "-q"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@e.st"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "commit", "--allow-empty", "-m", "init"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")

    orig = _sp.run
    VENDORS = ("claude", "codex", "agy")

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith(VENDORS):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return orig(cmd, *a, **k)

    monkeypatch.setattr(_sp, "run", fake_run)
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    out = implement("T1", _task(), str(wt), vendor="claude", seq=seq)
    seq.stop()
    assert out["ok"] is True
    evs = Ledger(str(tmp_path / "events.jsonl")).load_flat()
    types = {e["type"] for e in evs}
    assert "artifact.produced" in types
    assert "task.implemented" in types


def test_implement_records_self_score(monkeypatch, tmp_path):
    """The implementer's trailing `{"self_score": {...}}` JSON (see
    implementer._fmt_rubric) is recovered from the vendor's envelope output
    and recorded on task.implemented — this is a same-shot quality signal,
    not the harness's own verdict (that stays in review_flow/adjudicate)."""
    import json as _json
    import subprocess as _sp
    from harness.core.ledger import Ledger, Sequencer
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "wclite").mkdir()
    (wt / "wclite" / "core.py").write_text("x=1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "init", "-q"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@e.st"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "commit", "--allow-empty", "-m", "init"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")

    expected_score = {"total": 88, "threshold": 75, "breakdown": []}
    inner = _json.dumps({"self_score": expected_score})
    envelope = _json.dumps({"result": f"done.\n{inner}"})

    orig = _sp.run
    VENDORS = ("claude", "codex", "agy")

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith(VENDORS):
            class R:
                returncode = 0
                stdout = envelope
                stderr = ""
            return R()
        return orig(cmd, *a, **k)

    monkeypatch.setattr(_sp, "run", fake_run)
    task = _task()
    task["rubric"] = [{"criterion": "x", "weight": 100}]
    task["rubric_threshold"] = 75
    seq = Sequencer(str(tmp_path / "events.jsonl"))
    seq.start()
    out = implement("T1", task, str(wt), vendor="claude", seq=seq)
    seq.stop()
    assert out["ok"] is True
    assert out["self_score"] == expected_score
    evs = Ledger(str(tmp_path / "events.jsonl")).load_flat()
    impl_ev = next(e for e in evs if e["type"] == "task.implemented")
    assert impl_ev["self_score"] == expected_score


def test_implement_self_score_none_when_vendor_output_unparseable(monkeypatch, tmp_path):
    """No rubric / no parseable JSON from the vendor -> self_score is None,
    never an error (implement() must still succeed on the commit)."""
    import subprocess as _sp
    monkeypatch.chdir(REPO)
    from harness.roles.implementer import implement
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "wclite").mkdir()
    (wt / "wclite" / "core.py").write_text("x=1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "init", "-q"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@e.st"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", "-C", str(wt), "commit", "--allow-empty", "-m", "init"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")

    orig = _sp.run
    VENDORS = ("claude", "codex", "agy")

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith(VENDORS):
            class R:
                returncode = 0
                stdout = "plain text, no JSON here"
                stderr = ""
            return R()
        return orig(cmd, *a, **k)

    monkeypatch.setattr(_sp, "run", fake_run)
    out = implement("T1", _task(), str(wt), vendor="claude")
    assert out["ok"] is True
    assert out["self_score"] is None
