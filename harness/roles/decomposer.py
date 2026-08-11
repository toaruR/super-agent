#!/usr/bin/env python
"""Decomposer role (Stage 2, §9 step ②).

Turns a requirement (or an existing design) into a task DAG with acceptance
criteria, then runs the §6.2 structural acceptance contract before recording
anything on the ledger.

Structural checks (machine-enforced, before any LLM re-prompt):
  - acceptance[] is non-empty            (no unverifiable tasks)
  - every acceptance[].verb in verifiers.yaml  (H2: no injection path)
  - acceptance[].args is a list (executable form)
  - DAG has no cycles
  - touch_allow sets do not overlap across parallel tasks
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from harness.core.invoke import invoke, load_vendors
from harness.core.verifiers import VerifierRegistry

DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "goal": {"type": "string"},
                    "acceptance": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "verb": {"type": "string"},
                                "args": {"type": "array", "items": {"type": "string"}},
                                "expect_exit": {"type": "integer"},
                            },
                            "required": ["verb", "args"],
                        },
                    },
                    "touch_allow": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "rubric": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "criterion": {"type": "string"},
                                "weight": {"type": "integer"},
                            },
                            "required": ["criterion", "weight"],
                        },
                    },
                    "rubric_threshold": {"type": "integer"},
                },
                "required": ["task_id", "goal", "acceptance"],
            },
        }
    },
    "required": ["tasks"],
}

DECOMPOSE_PROMPT = """あなたはタスク分解担当です。要求（と既存の設計）から、実装タスクの DAG を作ってください。

ルール:
- 各タスクは acceptance（検証方法）を持つこと。acceptance は verb+args の形。
  verb は以下のいずれか: {verbs}
- 検証できないタスクは作らない（acceptance は空にしない）。
- depends_on で依存を明示（DAG）。循環は作らない。
- touch_allow は「このタスクが触ってよいファイル」を列挙（パス単位）。
  ファイル分割や新規ファイル作成が見込まれる場合は、以下のどちらかで対応すること:
  (a) 作成される見込みの新規ファイルパスを具体的に予測して列挙する
      （例: `harness/roles/new_helper.py`）。
  (b) 予測が難しいほど不確実な場合のみ、親ディレクトリをスラッシュ終わりで指定する
      （例: `harness/roles/`）。ただしディレクトリ指定はその配下の全ファイルへの
      アクセスを許可することになるため、他タスクとの並列実行を妨げやすい
      （同じディレクトリ配下に触れる他タスクと touch_allow 重複とみなされる）。
      本当に必要な場合以外は (a) の具体的なファイル名指定を優先すること。
  (c) 【重要・禁止事項】自身の acceptance で検証・実行するテストファイル（例: `tests/test_foo.py`）は、
      touch_allow に絶対含めてはいけません（受入基準テストの書き換え誤魔化し防止のためエラーになります）。
      touch_allow には実装対象のプログラムファイルのみを指定してください。

acceptance を作る際の重要な指針（実装者のワンショット成功率を上げるため）:
- 目標（goal）に複数の要素・振る舞いが含まれる場合、acceptance を1本にまとめず、
  要素ごとに分けて複数本用意すること。
- pytest の acceptance は `args` に `-k <test関数名>` を含め、その要素が何を検証するのか
  テスト関数名からわかるようにすること（例: 目標が「進捗率サマリー・タスク一覧・
  マイルストーンログを出力する」なら、acceptance を
  `pytest tests/test_x.py -k test_shows_progress_summary`,
  `pytest tests/test_x.py -k test_shows_task_list`,
  `pytest tests/test_x.py -k test_shows_milestone_log` の3本に分ける）。
- これにより実装者は「目標の各要素に対応するテストを個別に書いて満たす」ことを
  強制され、1本の曖昧なテストで誤魔化せなくなる。
- lint/型検査などのコード品質チェックは目的ではない。あくまで目標に書かれた
  振る舞いをコードが実現しているかどうかの検証に集中すること。

rubric（実装者の自己採点用の質的採点基準）について:
- acceptance（pytest等の自動テスト）は exit_code の pass/fail しか見ないため、
  「テストの主張を通すことだけを目的にテスト自体やアサーションを緩める／書き換える」
  誤魔化しを検出できない。rubric はこれを補うための、acceptance とは別の質的観点。
- 各タスクに rubric を2〜5項目、重み（weight, 合計100）付きで用意すること。例:
  - 「acceptance のテストファイル・アサーションを一切変更していない」（重み高め、目安20〜30）
  - 「goal に書かれた振る舞いをテストが直接検証していない部分（エッジケース、異常系）も
    自分で考慮し実装している」
  - 「touch_allow の範囲外に一切触れていない」
  - タスク固有の質的観点（可読性、既存コードとの整合性 等）
- rubric_threshold（合格ライン、100点満点中の目安70〜85）も指定すること。

出力は説明文や前置き・解説テキストを一切含めず、要求された JSON スキーマに従う純粋な JSON オブジェクトのみを出力すること。

要求: {requirement}
既存の設計: {existing}
"""


def _check_verbs(tasks: list[dict], registry: VerifierRegistry) -> list[str]:
    errs = []
    for t in tasks:
        for a in t.get("acceptance", []):
            verb = a.get("verb", "")
            if registry.resolve({"verb": verb, "args": a.get("args", [])}, ".") is None:
                errs.append(f"{t['task_id']}: verb '{verb}' は verifiers に未登録")
            if not isinstance(a.get("args"), list):
                errs.append(f"{t['task_id']}: acceptance.args がリストではない")
    return errs


def _check_dag(tasks: list[dict]) -> list[str]:
    """Detect cycles via DFS."""
    errs = []
    ids = {t["task_id"] for t in tasks}
    adj = {t["task_id"]: list(t.get("depends_on", [])) for t in tasks}
    # unknown dependency
    for t in tasks:
        for d in t.get("depends_on", []):
            if d not in ids:
                errs.append(f"{t['task_id']}: depends_on '{d}' が存在しない")
    # cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in adj}
    def dfs(u, stack):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                errs.append(f"DAG 循環: {' -> '.join(stack + [u, v])}")
            elif color.get(v, WHITE) == WHITE:
                dfs(v, stack + [u])
        color[u] = BLACK
    for tid in adj:
        if color[tid] == WHITE:
            dfs(tid, [])
    return errs


def touch_overlaps(p: str, q: str) -> bool:
    """True if two touch_allow entries overlap: exact match, or one is a
    directory scope (e.g. `harness/roles/`) containing the other's path
    (e.g. `harness/roles/foo.py`). Two distinct exact file paths under the
    same directory (neither is a directory scope of the other) do NOT
    overlap."""
    if p == q:
        return True
    return q.startswith(p.rstrip("/") + "/") or p.startswith(q.rstrip("/") + "/")


def _check_touch_overlap(tasks: list[dict]) -> list[str]:
    """Parallel tasks (no depends_on link) must not share touch_allow paths
    (exact match, or one entry's directory scope containing the other's)."""
    errs = []
    for i in range(len(tasks)):
        for j in range(i + 1, len(tasks)):
            a, b = tasks[i], tasks[j]
            a_paths = a.get("touch_allow", []) or []
            b_paths = b.get("touch_allow", []) or []
            if any(touch_overlaps(p, q) for p in a_paths for q in b_paths):
                if b["task_id"] not in a.get("depends_on", []) and \
                   a["task_id"] not in b.get("depends_on", []):
                    errs.append(
                        f"touch_allow 重複: {a['task_id']} と {b['task_id']} "
                        f"(並列だが範囲が被る)")
    return errs


# Verbs that run a specific test-definition file (as opposed to mypy/ruff,
# whose args name the *implementation* file under check, which legitimately
# belongs in touch_allow).
_TEST_VERBS = {"pytest", "unittest", "node-test", "jest", "vitest", "phpunit"}


def _acceptance_test_paths(task: dict) -> list[str]:
    """File-like args from test-runner acceptance criteria (the test
    definitions that decide pass/fail for this task)."""
    out = []
    for a in task.get("acceptance", []):
        if a.get("verb") not in _TEST_VERBS:
            continue
        for tok in a.get("args", []):
            if tok.startswith("-"):
                continue
            if "/" in tok or "\\" in tok:
                out.append(tok)
    return out


def _check_test_protection(tasks: list[dict]) -> list[str]:
    """touch_allow must not let a task edit the very test file(s) that grade
    it (H2-adjacent: otherwise the implementer can "pass" by weakening the
    test instead of fixing the code, see CLAUDE.md / this session's design
    discussion on implementer self-scoring)."""
    errs = []
    for t in tasks:
        ta = t.get("touch_allow", []) or []
        for ap in _acceptance_test_paths(t):
            for tp in ta:
                if touch_overlaps(tp, ap):
                    errs.append(
                        f"{t['task_id']}: touch_allow '{tp}' が自身の受入基準の"
                        f"テストファイル '{ap}' と重なる（実装者がテストを書き換えて"
                        f"誤魔化せてしまう）")
    return errs


def structural_check(tasks: list[dict], registry: VerifierRegistry) -> list[str]:
    """Return a list of error strings (empty = pass)."""
    errs = []
    if not tasks:
        errs.append("タスクが0件")
    for t in tasks:
        if not t.get("acceptance"):
            errs.append(f"{t['task_id']}: acceptance が空（検証不能なタスク）")
    errs += _check_verbs(tasks, registry)
    errs += _check_dag(tasks)
    errs += _check_touch_overlap(tasks)
    errs += _check_test_protection(tasks)
    return errs


def render_tasks_md(tasks: list[dict], requirement: str = "") -> str:
    """Render the decomposed task DAG as a human-readable Markdown file."""
    lines = ["# タスク分解（decompose 出力）", ""]
    if requirement:
        lines.append(f"要求: {requirement}")
        lines.append("")
    lines.append(f"タスク数: {len(tasks)}")
    lines.append("")
    for i, t in enumerate(tasks, 1):
        lines.append(f"## {i}. {t['task_id']}")
        lines.append("")
        lines.append(f"- 目標: {t.get('goal', '')}")
        deps = t.get("depends_on", [])
        lines.append(f"- 依存: {', '.join(deps) if deps else '（なし）'}")
        ta = t.get("touch_allow", [])
        if ta:
            lines.append(f"- 触ってよい範囲: {', '.join(ta)}")
        acc = t.get("acceptance", [])
        lines.append(f"- 受入基準 ({len(acc)}):")
        for a in acc:
            lines.append(f"  - `{a.get('verb', '')}` {' '.join(a.get('args', []))}"
                         f" (expect_exit={a.get('expect_exit', 0)})")
        rubric = t.get("rubric", [])
        if rubric:
            threshold = t.get("rubric_threshold", 80)
            lines.append(f"- 採点基準 (rubric, 合格ライン: {threshold}点):")
            for r in rubric:
                lines.append(f"  - {r.get('criterion', '')} (配点: {r.get('weight', 0)})")
        lines.append("")
    return "\n".join(lines) + "\n"


def parse_tasks_md(path: str) -> list[dict]:
    """Reverse of render_tasks_md: read a task DAG back from the Markdown file.

    Used by `plan --task_file <existing.md>` to skip re-decomposition. Returns a
    list of task dicts. Roughly parses our own emitted format.
    """
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    tasks: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^##\s+\d+\.\s+(\S+)", line)
        if m:
            if cur is not None:
                tasks.append(cur)
            cur = {"task_id": m.group(1), "acceptance": [], "depends_on": [],
                   "touch_allow": [], "rubric": []}
            continue
        if cur is None:
            continue
        if line.startswith("- 目標:"):
            cur["goal"] = line[len("- 目標:"):].strip()
        elif line.startswith("- 依存:"):
            deps = line[len("- 依存:"):].strip()
            if deps and deps != "（なし）":
                cur["depends_on"] = [d.strip() for d in deps.split(",") if d.strip()]
        elif line.startswith("- 触ってよい範囲:"):
            ta = line[len("- 触ってよい範囲:"):].strip()
            if ta:
                cur["touch_allow"] = [t.strip() for t in ta.split(",") if t.strip()]
        elif line.startswith("- 採点基準"):
            m3 = re.search(r"合格ライン:\s*(\d+)点", line)
            cur["rubric_threshold"] = int(m3.group(1)) if m3 else 80
        elif re.match(r"^\s*-\s+.+\(配点:\s*\d+\)\s*$", line):
            m3 = re.match(r"^\s*-\s+(.*?)\s*\(配点:\s*(\d+)\)\s*$", line)
            if m3:
                cur["rubric"].append({"criterion": m3.group(1), "weight": int(m3.group(2))})
        elif line.lstrip().startswith("- `") and "`" in line[line.find("`")+1:]:
            # acceptance bullet: `verb` args... (expect_exit=N)
            # only the verb is backticked; args follow after the closing backtick
            tick = line.find("`")
            close = line.find("`", tick + 1)
            verb = line[tick+1:close].strip()
            rest = line[close+1:].strip()
            expect = "0"
            # strip the (expect_exit=N) suffix wherever it sits
            if "(expect_exit=" in rest:
                head, _, tail = rest.partition("(expect_exit=")
                args_part = head.strip()
                expect = tail.rstrip(")").strip()
            else:
                args_part = rest
            args = args_part.split()
            cur["acceptance"].append({
                "verb": verb,
                "args": args,
                "expect_exit": int(expect) if str(expect).isdigit() else 0,
            })
    if cur is not None:
        tasks.append(cur)
    return tasks


def _normalize_tasks(tasks: list[dict]) -> None:
    """Coerce loose LLM outputs (e.g. 'id' instead of 'task_id', acceptance item as string 'pytest tests/foo.py'
    instead of {'verb': 'pytest', 'args': ['tests/foo.py']}) into proper dicts."""
    for i, t in enumerate(tasks, 1):
        tid = t.get("task_id") or t.get("id") or f"task-{i}"
        t["task_id"] = tid

        acc = t.get("acceptance", [])
        norm_acc = []
        for a in acc:
            if isinstance(a, str):
                parts = a.split()
                if parts:
                    norm_acc.append({"verb": parts[0], "args": parts[1:], "expect_exit": 0})
            elif isinstance(a, dict):
                args = a.get("args", [])
                if isinstance(args, str):
                    args = args.split()
                norm_acc.append({
                    "verb": a.get("verb", ""),
                    "args": args,
                    "expect_exit": a.get("expect_exit", 0),
                })
        t["acceptance"] = norm_acc

        rubric = t.get("rubric", [])
        norm_rub = []
        for r in rubric:
            if isinstance(r, str):
                norm_rub.append({"criterion": r, "weight": 25})
            elif isinstance(r, dict):
                c = r.get("criterion") or r.get("criteria") or ""
                w = r.get("weight", 20)
                norm_rub.append({"criterion": c, "weight": w})
        t["rubric"] = norm_rub

        # Remove any acceptance test files from touch_allow (test protection contract)
        test_paths = set(_acceptance_test_paths(t))
        if test_paths:
            ta = t.get("touch_allow", []) or []
            t["touch_allow"] = [tp for tp in ta if not any(touch_overlaps(tp, ap) for ap in test_paths)]


def decompose(task_id: str, requirement: str, vendor: str = "claude",
              existing_design: str = "", dry_run: bool = False,
              seq=None, model: str | None = None, effort: str | None = None,
              design_file: str = "", timeout: int | None = None) -> dict:
    """Decompose a requirement into a checked task DAG. Returns the payload.

    ledger events: task.created per task (after structural check passes).
    If the check fails, returns {"ok": False, "errors": [...]} and records nothing.

    design_file, if given, tags every ledger event so this chunk lands in the
    same (design_file, task_file) chunk as the caller's other events instead
    of splintering off into a design_file="" chunk (see CLAUDE.md's
    "ハマりポイント" on decompose()'s historical design_file-loss bug).
    """
    emit = (lambda tid, typ, **kw: seq.propose(tid, typ, design_file=design_file, **kw)) \
        if seq is not None else (lambda tid, typ, **kw: None)
    invoke_kwargs = {"timeout": timeout} if timeout is not None else {}
    config_dir = Path(__file__).resolve().parent.parent / "config"
    registry = VerifierRegistry(config_dir / "verifiers.yaml")
    verbs = ", ".join(sorted(registry._map.keys()))

    if dry_run:
        decls = load_vendors(config_dir)
        decl = decls.get(vendor, decls["claude"])
        prompt = DECOMPOSE_PROMPT.format(requirement=requirement, existing=existing_design, verbs=verbs)
        res = invoke(decl, prompt, schema=DECOMPOSE_SCHEMA, model=model, effort=effort, role="design", dry_run=True, **invoke_kwargs)
        return {"ok": True, "dry_run": True, "cmd": res.get("cmd")}

    decls = load_vendors(config_dir)
    decl = decls.get(vendor, decls["claude"])
    prompt = DECOMPOSE_PROMPT.format(requirement=requirement, existing=existing_design, verbs=verbs)
    res = invoke(decl, prompt, schema=DECOMPOSE_SCHEMA, model=model, effort=effort, role="design", dry_run=False, **invoke_kwargs)
    parsed = res.get("result") or {}
    if isinstance(parsed, str):
        # vendor returned a JSON string instead of a parsed object
        try:
            import json as _json
            parsed = _json.loads(parsed)
        except Exception:
            parsed = {}
    tasks = parsed.get("tasks", []) if isinstance(parsed, dict) else []
    _normalize_tasks(tasks)

    errs = structural_check(tasks, registry)
    if errs:
        emit(task_id, "decompose.rejected", errors=errs)
        return {"ok": False, "errors": errs, "tasks": tasks}

    emit(task_id, "decompose.ok", n_tasks=len(tasks))
    for t in tasks:
        emit(t["task_id"], "task.created",
             goal=t.get("goal", ""),
             acceptance=t.get("acceptance", []),
             depends_on=t.get("depends_on", []),
             touch_allow=t.get("touch_allow", []),
             rubric=t.get("rubric", []),
             rubric_threshold=t.get("rubric_threshold", 80))
    return {"ok": True, "tasks": tasks}
