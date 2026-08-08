"""Test session setup for the harness test-suite.

We set SUPER_AGENT_TEST=1 so that drive() skips its real git checkout/stash
of the caller's working tree. A test invocation must never mutate the repo
the developer is working in (otherwise a test could stash the dev's
uncommitted changes and leave them on the wrong branch).
"""
import os

os.environ.setdefault("SUPER_AGENT_TEST", "1")
