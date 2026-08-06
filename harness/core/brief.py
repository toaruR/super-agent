#!/usr/bin/env python
"""Brief builder wrapper (Stage C). Delegates to probe/n3/brief.py's build().

build(ev, changed, context, budget_tokens, root) -> (text, stats)
The reviewer is told to judge only from the brief (no execution) - this is
what keeps the judgment environment-independent (E-4 / ARCHITECTURE §7.2).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BRIEF_MODULE_PATH = _REPO_ROOT / "probe" / "n3" / "brief.py"

_spec = importlib.util.spec_from_file_location("_probe_brief", _BRIEF_MODULE_PATH)
_probe_brief = importlib.util.module_from_spec(_spec)
sys.modules["_probe_brief"] = _probe_brief
_spec.loader.exec_module(_probe_brief)


def build(ev, changed, context, budget_tokens, root) -> tuple[str, dict]:
    return _probe_brief.build(ev, changed, context, budget_tokens, root)
