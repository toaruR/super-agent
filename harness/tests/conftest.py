"""Test session setup for the harness test-suite.

We set SUPER_AGENT_TEST=1 so that drive() skips its real git checkout/stash
of the caller's working tree. A test invocation must never mutate the repo
the developer is working in (otherwise a test could stash the dev's
uncommitted changes and leave them on the wrong branch).
"""
import os
from pathlib import Path
import pytest

os.environ.setdefault("SUPER_AGENT_TEST", "1")

REPO = Path(__file__).resolve().parents[2]
SHARED_LEDGER = REPO / "harness" / "ledger" / "events.jsonl"


@pytest.fixture(autouse=True)
def preserve_shared_ledger():
    """Preserve and restore the shared harness/ledger/events.jsonl across tests so that
    tests modifying or unlinking the shared ledger file leave it intact afterwards."""
    had = SHARED_LEDGER.exists()
    backup_bytes = SHARED_LEDGER.read_bytes() if had else None
    try:
        yield
    finally:
        if backup_bytes is not None:
            SHARED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
            SHARED_LEDGER.write_bytes(backup_bytes)
        elif SHARED_LEDGER.exists():
            SHARED_LEDGER.unlink()
