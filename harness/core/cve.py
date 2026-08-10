#!/usr/bin/env python
"""CVE wrapper (Stage C). Routes the allowlisted acceptance verbs (H2) to
concrete command lists, then runs the probe + acceptance in the CVE.

Low-level execution reuses probe/n3/cve.py's `verify` (which never decides
pass/fail - that is the adjudicator's job). probe/ is treated as an evidence
archive, so we load it by file path rather than as a package. The
verb->argv translation goes through VerifierRegistry so the injection path
(H2) stays closed.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness.core.verifiers import VerifierRegistry

# locate probe/n3/cve.py relative to this file's repo root (src/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CVE_MODULE_PATH = _REPO_ROOT / "probe" / "n3" / "cve.py"


def _load_probe_cve():
    spec = importlib.util.spec_from_file_location("_probe_cve", _CVE_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_probe_cve"] = mod
    spec.loader.exec_module(mod)
    return mod


_probe_cve = _load_probe_cve()

# Node系 verb（verifiers.yaml 参照）。node_modules 未インストールだと
# 「実装が間違っている」のか「依存が入っていないだけ」なのか証拠から
# 区別できず、レビュアが誤って fail 判定しかねないので実行前に確認する。
_NODE_VERBS = {"node-test", "jest", "vitest", "tsc", "eslint"}


def _ensure_node_deps(root: Path, acceptance: list[dict[str, Any]]) -> None:
    """package.json はあるが node_modules が無い場合、TTY なら npm install を
    y/N で確認する。非対話実行（drive の自動ループ等）では確認せず警告のみ
    出して先へ進む（検証は自然に失敗し、証拠として記録される）。"""
    if not any(a.get("verb") in _NODE_VERBS for a in acceptance):
        return
    if not (root / "package.json").exists():
        return
    if (root / "node_modules").exists():
        return
    if sys.stdin.isatty():
        ans = input(
            f"[cve] {root} に node_modules がありません。npm install を実行しますか? [y/N]: "
        ).strip().lower()
        if ans == "y":
            subprocess.run(["npm", "install"], cwd=str(root), shell=False)
            return
    print(
        f"[warn] {root}: node_modules が見つかりません。検証前に `npm install` を"
        f" 実行してください（node-test/jest/vitest/tsc/eslint はこのまま失敗します）。",
        file=sys.stderr,
    )


class CVE:
    def __init__(self, cfg_path: str | Path, verifiers_path: str | Path) -> None:
        import yaml

        cfg_path = Path(cfg_path)
        if not cfg_path.exists():
            # 実環境用の verification_env.yaml が無い場合はサンプルへフォールバック。
            # （サンプルはパスを一般化しているため、環境に合わせた実ファイルを
            #  `verification_env_sample.yaml` からコピーして作成することを推奨）
            sample = cfg_path.with_name("verification_env_sample.yaml")
            if sample.exists():
                import sys
                print(f"[warn] {cfg_path.name} not found; using {sample.name} "
                      f"(copy it to {cfg_path.name} and set your venv python path)",
                      file=sys.stderr)
                cfg_path = sample
        with open(cfg_path, "r", encoding="utf-8") as fh:
            self.cfg = yaml.safe_load(fh)
        self._verifiers = VerifierRegistry(verifiers_path)

    def _acceptance_to_argv(self, acceptance: list[dict[str, Any]]) -> list[list[str]]:
        argv: list[list[str]] = []
        for acc in acceptance:
            resolved = self._verifiers.resolve(acc, cwd=".")
            if resolved is None:
                # unknown verb -> refuse to build a command (H2; structural check)
                raise ValueError(f"verb not whitelisted: {acc.get('verb')!r}")
            argv.append(resolved[0])
        return argv

    def run(self, root: str | Path, acceptance: list[dict[str, Any]]) -> dict[str, Any]:
        root = Path(root)
        _ensure_node_deps(root, acceptance)
        probe = [list(c) for c in self.cfg.get("probe", [])]
        argv = self._acceptance_to_argv(acceptance)
        return _probe_cve.verify(root, argv, probe)

    def hash(self, root: str | Path) -> str:
        return _probe_cve.tree_hash(Path(root))
