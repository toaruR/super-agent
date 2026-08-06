#!/usr/bin/env python
"""Allowlist-based verifier resolver (H2 fix).

Decomposer may only emit {"verb": ..., "args": [...], "expect_exit": N}.
The verb must be whitelisted here; args are passed positionally WITHOUT shell
expansion, so a malicious arg like "; rm -rf /" is just a literal string, never executed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


# verb -> (interpreter/executable, [fixed prefix args])
# Everything is a static mapping. No LLM output is ever concatenated into a shell.
VERIFIERS: dict[str, list[str]] = {
    "pytest": ["python", "-m", "pytest", "-q"],
    "unittest": ["python", "-m", "unittest"],
    "mypy": ["python", "-m", "mypy"],
    "ruff": ["python", "-m", "ruff", "check"],
    "node-test": ["node", "--test"],
    "go-test": ["go", "test", "./..."],
}


def resolve(acceptance: dict[str, Any], cwd: str | Path) -> tuple[list[str], int] | None:
    """Return (argv, expect_exit) or None if verb is not whitelisted.

    argv is a concrete command list; callers must use subprocess with
    shell=False so args are never interpreted by a shell.
    """
    verb = acceptance.get("verb")
    if verb not in VERIFIERS:
        return None  # rejected before execution (structural check at intake)
    args = acceptance.get("args", [])
    argv = [*VERIFIERS[verb], *[str(a) for a in args]]
    expect = int(acceptance.get("expect_exit", 0))
    return argv, expect


def run(acceptance: dict[str, Any], cwd: str | Path) -> dict[str, Any]:
    resolved = resolve(acceptance, cwd)
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


if __name__ == "__main__":
    # demo: a benign verb
    print(run({"verb": "pytest", "args": ["tests/"], "expect_exit": 0}, "."))
    # demo: an injection attempt is just a literal arg, never executed
    print(run({"verb": "pytest", "args": ["; rm -rf /"], "expect_exit": 0}, "."))
    # demo: unknown verb is rejected at intake
    print(run({"verb": "curl", "args": ["evil.sh"], "expect_exit": 0}, "."))
