#!/usr/bin/env python
"""CVE - the canonical verification environment.

The ONE place where anything is executed. Reviewers never run commands;
they read the evidence this produces.

Fixes H4 (evidence not bound to the artifact): every evidence bundle carries
a tree_hash over the exact files that were verified. A reviewer's finding can
therefore be tied to a specific state of the code.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


def tree_hash(root: Path, patterns=("**/*.py",)) -> str:
    h = hashlib.sha256()
    files = sorted({p for pat in patterns for p in root.glob(pat) if p.is_file()})
    for p in files:
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def run(cmd: list[str], cwd: Path, timeout: int = 120) -> dict:
    t0 = time.time()
    try:
        cp = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return {
            "cmd": " ".join(cmd),
            "exit_code": cp.returncode,
            "stdout": cp.stdout[-4000:],
            "stderr": cp.stderr[-2000:],
            "ms": int((time.time() - t0) * 1000),
        }
    except FileNotFoundError as e:
        return {"cmd": " ".join(cmd), "exit_code": None, "error": f"ENOENT: {e}", "ms": 0}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "exit_code": None, "error": "timeout", "ms": timeout * 1000}


def verify(root: Path, acceptance: list[list[str]], probe: list[list[str]]) -> dict:
    """Returns an evidence bundle. Never decides pass/fail - that is the adjudicator."""
    probes = [run(c, root) for c in probe]
    if any(p.get("exit_code") != 0 for p in probes):
        return {
            "cve_ok": False,
            "tree_hash": tree_hash(root),
            "probes": probes,
            "evidence": [],
        }
    ev = []
    for i, c in enumerate(acceptance, 1):
        r = run(c, root)
        r["id"] = f"E-{i}"
        ev.append(r)
    return {
        "cve_ok": True,
        "tree_hash": tree_hash(root),
        "probes": probes,
        "evidence": ev,
    }


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    cfg = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    out = verify(root, cfg["acceptance"], cfg.get("probe", []))
    print(json.dumps(out, ensure_ascii=False, indent=2))
