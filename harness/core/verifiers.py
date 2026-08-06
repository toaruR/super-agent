#!/usr/bin/env python
"""Allowlist-based verifier resolver (H2 fix).

Decomposer may only emit {"verb": ..., "args": [...], "expect_exit": N}.
The verb must be whitelisted in config/verifiers.yaml; args are passed
positionally WITHOUT shell expansion, so a malicious arg like "; rm -rf /"
is just a literal string, never executed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml


def load_verifiers(path: str | Path) -> dict[str, list[str]]:
    """Load verb -> argv-prefix mapping from YAML config."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {k: list(v) for k, v in data.get("verifiers", {}).items()}


class VerifierRegistry:
    def __init__(self, cfg_path: str | Path) -> None:
        self._map = load_verifiers(cfg_path)

    def resolve(self, acceptance: dict[str, Any], cwd: str | Path) -> tuple[list[str], int] | None:
        """Return (argv, expect_exit) or None if verb is not whitelisted.

        argv is a concrete command list; callers must use subprocess with
        shell=False so args are never interpreted by a shell.
        """
        verb = acceptance.get("verb")
        if verb not in self._map:
            return None  # rejected before execution (structural check at intake)
        args = acceptance.get("args", [])
        argv = [*self._map[verb], *[str(a) for a in args]]
        expect = int(acceptance.get("expect_exit", 0))
        return argv, expect

    def run(self, acceptance: dict[str, Any], cwd: str | Path) -> dict[str, Any]:
        resolved = self.resolve(acceptance, cwd)
        if resolved is None:
            return {"ok": False, "rejected": True, "reason": "verb not whitelisted"}
        argv, expect = resolved
        # shell=False: args are literal. A "rm -rf /" arg stays a string.
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, shell=False)
        return {
            "ok": proc.returncode == expect,
            "exit_code": proc.returncode,
            "expect_exit": expect,
            "argv": argv,
            "stdout": proc.stdout[-1500:],
            "stderr": proc.stderr[-1500:],
        }
