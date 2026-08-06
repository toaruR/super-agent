#!/usr/bin/env python
"""Adjudicator wrapper (Stage C). Delegates to probe/n3/adjudicate2.py's
adjudicate(). The verdict depends ONLY on CVE evidence (tree_hash bound),
never on the reviewer's execution environment (E-4 / ARCHITECTURE §7.2).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ADJ_MODULE_PATH = _REPO_ROOT / "probe" / "n3" / "adjudicate2.py"

_spec = importlib.util.spec_from_file_location("_probe_adj", _ADJ_MODULE_PATH)
_probe_adj = importlib.util.module_from_spec(_spec)
sys.modules["_probe_adj"] = _probe_adj
_spec.loader.exec_module(_probe_adj)


def adjudicate(evidence: dict, review: dict | None) -> dict:
    return _probe_adj.adjudicate(evidence, review)
