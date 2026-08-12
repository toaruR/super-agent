#!/usr/bin/env python
"""Implementer role (Stage 4, §9 step ④).

Runs the Implementer vendor inside a task's git worktree, constrained to the
task's `touch_allow` paths, then commits the result and records it on the
ledger as `artifact.produced` (+ `task.implemented`).

§3.1 isolation: the vendor only ever sees its own worktree; §6.2 `touch_allow`
is the allow-list it may modify. The harness (not the vendor) performs the
commit so the evidence (`tree_hash`/commit) is bound deterministically.

NOTE (agy / cwd-ignoring vendors): some vendors (agy) ignore the process cwd
and write to a fixed scratch dir unless the prompt states an absolute path.
So the prompt is built with `{worktree}` and instructs the vendor to write to
`<worktree>/<file>` using the absolute path. `--dangerously-skip-permissions`
is required for agy (placed before `--print`). See vendors.yaml.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from harness.core.invoke import invoke, load_vendors, normalize_model, extract_result, DEFAULT_TIMEOUT
from harness.core.ledger import Sequencer
from harness.core.progress import write_progress

IMPLEMENT_PROMPT = """\
あなたは Implementer です。以下のタスクを実装してください。

# 作業ディレクトリ（絶対パス）
{worktree}

# 設計（参考。プロジェクト全体の意図・DAG全体像を把握するための文脈。
# 矛盾する場合は下記のタスク定義（目標・受入基準・touch_allow）を優先すること）
{design_context}

# タスク
タスクID: {task_id}
目標: {goal}

# 受入基準（これらが通ること）
{acceptance}

# 触ってよい範囲（このリストのファイル以外は作成・変更してはいけない）
{touch_allow}

{rubric_section}

# 制約
- 上記 `touch_allow` に列挙されたファイル・パスだけを作成・変更すること。
- それ以外のファイル（設定、他タスクのファイル、README 等）には触らないこと。
- コミットは harness が行うので、あなたはコミットしなくてよい。
- 実装が終わったら、受入基準を自分で確認できる範囲で満たしているか考えよ。
- 【最重要】すべてのファイルは **作業ディレクトリ {worktree} の絶対パス** に作成・変更すること。
  例えば `live_probe.txt` を作る場合は `{worktree}/live_probe.txt` を絶対パスで書き込む。
  相対パスや「カレントディレクトリ」への指定は無視され別の場所に書かれるため、必ず
  上記 {worktree} を接頭した絶対パスを使用すること（一部のエージェントは cwd を無視する）。
"""


def _fmt_acceptance(task: dict) -> str:
    out = []
    for a in task.get("acceptance", []):
        verb = a.get("verb", "")
        args = " ".join(a.get("args", []))
        out.append(f"- `{verb} {args}` (expect_exit={a.get('expect_exit', 0)})")
    return "\n".join(out) if out else "- （なし）"


def _fmt_design_context(design_context: str) -> str:
    text = (design_context or "").strip()
    return text if text else "（なし）"


def _fmt_rubric(task: dict) -> str:
    """Self-scoring rubric section (planner-authored, orthogonal to acceptance).

    acceptance only sees pass/fail exit codes, so it can't detect "made the
    test pass by weakening the test itself" style gaming. The rubric asks the
    implementer to keep iterating (re-running the real acceptance commands)
    within this same invocation until it self-scores >= threshold, then
    report the score as trailing JSON. This score is NOT the harness's
    verdict (review_flow/adjudicate still gate on real evidence independently)
    — it's a same-shot quality lever, not a substitute for it.
    """
    rubric = task.get("rubric", [])
    if not rubric:
        return ""
    threshold = task.get("rubric_threshold", 80)
    lines = [
        f"# 自己採点基準（rubric、合格ライン: {threshold}点/100点）",
        "実装が終わったら、以下の基準で自己採点し、合計が合格ラインに達するまで",
        "実装を改良し続けること（受入基準コマンドは自分で実際に再実行して確認すること）。",
        "touch_allow の範囲外、特に受入基準として使われているテストファイル自体を",
        "書き換えて点数を稼ぐことは禁止（無意味でもある。自己採点とは独立にハーネス側が",
        "実受入基準を再実行して裁定するため、テストを緩めても最終的な合否は変わらない）。",
        "",
    ]
    for r in rubric:
        lines.append(f"- {r.get('criterion', '')} (配点: {r.get('weight', 0)})")
    lines.append("")
    lines.append(
        "改良ループを終えたら、出力の一番最後の行に次のJSONを1行だけ出力すること"
        "（前後に余計な文字を付けない）:"
    )
    lines.append(
        '{"self_score": {"total": <0-100の整数>, "threshold": ' + str(threshold) +
        ', "breakdown": [{"criterion": "...", "score": <int>, "weight": <int>}, ...]}}'
    )
    return "\n".join(lines)


def _fmt_touch_allow(task: dict, worktree: str) -> str:
    """touch_allow を worktree の絶対パス付きで表示（agy 等が cwd を無視する対策）。"""
    out = []
    for p in task.get("touch_allow", []):
        if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
            out.append(f"- {p}")
        else:
            out.append(f"- {worktree}/{p}")
    return "\n".join(out) if out else "- （なし）"


def _extract_self_score(stdout: str, decl) -> dict | None:
    """Best-effort recovery of the implementer's trailing self-score JSON
    (see `_fmt_rubric`). Never raises; returns None if the vendor produced no
    parseable JSON, no `self_score` key, or the task had no rubric at all."""
    try:
        parsed = extract_result(stdout, decl.result_path())
    except Exception:
        return None
    if isinstance(parsed, dict):
        score = parsed.get("self_score")
        if isinstance(score, dict):
            return score
    return None


def implement(task_id: str, task: dict, worktree_path: str,
              vendor: str = "claude", seq: Sequencer | None = None,
              dry_run: bool = False, model: str | None = None,
              effort: str | None = None, design_file: str = "",
              design_context: str = "", timeout: int | None = None) -> dict:
    """Implement a single task inside its worktree and commit.

    design_context, if given, is the full design document text (drive()'s
    spec_text) so the implementer can see the overall intent/DAG context
    instead of only its own narrow goal string. Not to be confused with
    design_file (a path, used only for ledger chunk tagging).

    Returns a payload with ok/commit/cmd. Records ledger events when seq given.
    """
    draft_p = Path(worktree_path) / ".implement_draft"
    if draft_p.exists():
        draft_text = draft_p.read_text(encoding="utf-8", errors="ignore")
        notice = (
            f"\n\n【前回の実装試行での思考ログ・ドラフト（引き継ぎ用）】\n"
            f"---\n{draft_text}\n---\n"
            f"前回の試行内容を参考に、未完了の部分を完成させ、指示に従って実装を行ってください。"
        )
        design_context = (design_context + notice) if design_context else notice

    goal = task.get("goal", "")
    touch_allow = task.get("touch_allow", [])
    prompt = IMPLEMENT_PROMPT.format(
        task_id=task_id,
        goal=goal,
        acceptance=_fmt_acceptance(task),
        touch_allow=_fmt_touch_allow(task, worktree_path),
        worktree=worktree_path,
        design_context=_fmt_design_context(design_context),
        rubric_section=_fmt_rubric(task),
    )

    decls = load_vendors(Path(__file__).resolve().parent.parent / "config")
    decl = decls.get(vendor, decls["claude"])
    # Normalize known-bad model names (e.g. yaml `hy3:Free` -> `tencent/hy3:free`)
    # so the live vendor call never hits a 404. Code-side alias; yaml untouched.
    model = normalize_model(model)

    # Liveness heartbeat side-channel (docs/design/timeout-liveness-watchdog.md
    # §0/§3): task_id already includes the sub-channel name (e.g.
    # `PA__hermes_0`) when drive.py fans out multiple implement channels, so
    # it doubles as the progress-file key with no extra plumbing.
    progress_cb = None
    if seq is not None and not dry_run:
        ledger_path = seq.path
        seq.propose(task_id, "implementer.invoked", vendor=vendor, model=model, effort=effort, design_file=design_file)
        write_progress(task_id, ledger_path, vendor=vendor, status="running", detail="implementing task...")

        def progress_cb(detail: str) -> None:
            write_progress(task_id, ledger_path, vendor=vendor,
                           status="running", detail=detail)

    # run the vendor inside the worktree (invoke() owns build_command,
    # streaming/idle-timeout, and retry-on-content-block; see
    # harness/core/invoke.py)
    try:
        run = invoke(
            decl, prompt, model=model, effort=effort, role="implement",
            worktree=worktree_path, cwd=str(worktree_path),
            timeout=timeout or DEFAULT_TIMEOUT, dry_run=dry_run,
            progress_cb=progress_cb, draft_path=str(draft_p),
        )
    except FileNotFoundError as e:
        if seq is not None:
            seq.propose(task_id, "implementer.error", error=str(e), design_file=design_file)
        return {"ok": False, "task_id": task_id, "error": str(e)}
    except subprocess.TimeoutExpired as e:
        err = f"vendor subprocess timed out after {e.timeout}s"
        if seq is not None:
            seq.propose(task_id, "implementer.error", error=err, design_file=design_file)
        return {"ok": False, "task_id": task_id, "error": err}

    if dry_run:
        return {"ok": True, "dry_run": True, "cmd": run["cmd"], "task_id": task_id}

    if run["returncode"] != 0:
        err = (run["stderr"] or run["stdout"]).strip()
        if seq is not None:
            seq.propose(task_id, "implementer.error", error=err, design_file=design_file)
        if progress_cb is not None:
            write_progress(task_id, seq.path, vendor=vendor, status="error", detail=err[:200])
        return {"ok": False, "task_id": task_id, "error": err}

    self_score = _extract_self_score(run["stdout"], decl)

    # harness performs the commit (touch_allow allow-list)
    commit = _commit_worktree(task_id, worktree_path, touch_allow, seq)
    if not commit.get("ok"):
        if progress_cb is not None:
            write_progress(task_id, seq.path, vendor=vendor, status="error",
                           detail=str(commit.get("error"))[:200])
        return {"ok": False, "task_id": task_id,
                "error": commit.get("error"), "vendor_rc": run["returncode"]}

    if draft_p.exists():
        try:
            draft_p.unlink()
        except OSError:
            pass

    if seq is not None:
        seq.propose(task_id, "artifact.produced",
                    paths=touch_allow, commit=commit.get("commit"),
                    design_file=design_file)
        seq.propose(task_id, "task.implemented",
                    commit=commit.get("commit"),
                    tree_hash=commit.get("tree_hash"),
                    self_score=self_score,
                    design_file=design_file)

    if progress_cb is not None:
        write_progress(task_id, seq.path, vendor=vendor, status="done", detail="")

    return {
        "ok": True,
        "task_id": task_id,
        "commit": commit.get("commit"),
        "tree_hash": commit.get("tree_hash"),
        "paths": touch_allow,
        "cmd": run["cmd"],
        "self_score": self_score,
    }


def _commit_worktree(task_id: str, worktree_path: str, touch_allow: list[str],
                      seq: Sequencer | None) -> dict:
    """Stage the allow-listed paths in the worktree and commit.

    Uses `git -C <worktree>` so we never leave the worktree. If nothing
    changed, returns ok with commit=None (idempotent).
    """
    paths = touch_allow or ["."]
    from harness.core.invoke import git_executable
    git_bin = git_executable()
    add = [git_bin, "-C", str(worktree_path), "add", "--", *paths]
    try:
        ap = subprocess.run(add, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", shell=False)
        if ap.returncode != 0:
            return {"ok": False, "error": (ap.stderr or ap.stdout).strip()}
        # detect staged changes
        diff = subprocess.run(
            [git_bin, "-C", str(worktree_path), "diff", "--cached", "--quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        if diff.returncode == 0:
            # nothing staged -> nothing to commit
            return {"ok": True, "commit": None, "tree_hash": None}
        msg = f"{task_id}: implement\n\nallow-list: {', '.join(touch_allow) or '(all)'}"
        cp = subprocess.run(
            [git_bin, "-C", str(worktree_path), "commit", "-m", msg],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        if cp.returncode != 0:
            return {"ok": False, "error": (cp.stderr or cp.stdout).strip()}
        # capture commit hash + tree hash (evidence binding, §3.2)
        ch = subprocess.run(
            [git_bin, "-C", str(worktree_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        th = subprocess.run(
            [git_bin, "-C", str(worktree_path), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False)
        return {"ok": True,
                "commit": (ch.stdout or "").strip(),
                "tree_hash": (th.stdout or "").strip()}
    except FileNotFoundError:
        return {"ok": False, "error": "git executable not found in PATH"}
