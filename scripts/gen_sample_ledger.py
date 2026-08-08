#!/usr/bin/env python
"""Generate a sample ledger (harness/ledger/sample-events.jsonl) for manual
dashboard / status inspection. NOT appended to the real append-only ledger.

Chunk layout (spec.md "用語: 台帳の構造（1塊 = 1設計）"):
  one line per chunk: {"design_file": ..., "task_file": ..., "events": [ ... ]}

Includes:
  - real-evidence statuses (integrated / implemented / failed / leased / created)
  - speculative channel fan-out (same task, multiple channels; one winner)
  - dry-run events (tree_hash = "dry-run") to verify they are surfaced distinctly
  - a chunk with empty task_file (task not yet settled, e.g. require->decompose)
"""
from __future__ import annotations
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # scripts/ -> src/
SAMPLE_TASKS = HERE / "probe" / "samples" / "sample-tasks.md"
SAMPLE_DESIGN = HERE / "docs" / "design-notes" / "architecture-v3.md"


def ev(event_id: str, type_: str, **extra) -> dict:
    d = {"event_id": event_id, "type": type_}
    d.update(extra)
    return d


def main() -> None:
    chunks: list[dict] = []

    # --- Chunk 1: tasks settled (sample-tasks.md) with real evidence ---
    chunks.append({
        "design_file": str(SAMPLE_DESIGN),
        "task_file": str(SAMPLE_TASKS),
        "events": [
            ev("T1:1", "task.created", goal="g1"),
            ev("T1:2", "task.implemented", tree_hash="a1b2c3d4"),
            ev("T1:3", "verification.run", tree_hash="a1b2c3d4"),
            ev("T1:4", "judgment", tree_hash="a1b2c3d4", verdict="PASS"),
            ev("T1:5", "integrated", tree_hash="a1b2c3d4"),

            ev("T2:1", "task.created", goal="g2"),
            ev("T2:2", "task.implemented", tree_hash="b2c3d4e5"),

            ev("T3:1", "task.created", goal="g3"),
            ev("T3:2", "implementer.error", tree_hash="c3d4e5f6"),

            ev("T4:1", "task.created", goal="g4"),
            ev("T4:2", "task.leased", tree_hash="d4e5f6a7"),

            ev("T5:1", "task.created", goal="g5"),

            # speculative fan-out: hermes_0 wins, hermes_1 fails
            ev("T6:1", "task.created", goal="g6"),
            ev("T6__hermes_0:1", "task.created", goal="g6"),
            ev("T6__hermes_0:2", "task.implemented", tree_hash="f6a7b8c9"),
            ev("T6__hermes_0:3", "verification.run", tree_hash="f6a7b8c9"),
            ev("T6__hermes_0:4", "judgment", tree_hash="f6a7b8c9", verdict="PASS"),
            ev("T6__hermes_0:5", "integrated", tree_hash="f6a7b8c9"),
            ev("T6__hermes_1:1", "task.created", goal="g6"),
            ev("T6__hermes_1:2", "implementer.error", tree_hash="a7b8c9d0"),
        ],
    })

    # --- Chunk 2: dry-run noise (no real evidence) ---
    chunks.append({
        "design_file": str(SAMPLE_DESIGN),
        "task_file": str(SAMPLE_TASKS),
        "events": [
            ev("T7:1", "task.created", goal="g7", tree_hash="dry-run"),
            ev("T7:2", "verification.run", tree_hash="dry-run"),
            ev("T7:3", "judgment", tree_hash="dry-run", verdict="judgment_unavailable"),
        ],
    })

    # --- Chunk 3: task not yet settled (empty task_file) ---
    chunks.append({
        "design_file": str(SAMPLE_DESIGN),
        "events": [
            ev("REQ1:1", "task.created", goal="(requirement, pre-decompose)", role="decomposer"),
        ],
    })

    out = HERE / "harness" / "ledger" / "sample-events.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {len(chunks)} chunks to {out}")


if __name__ == "__main__":
    main()
