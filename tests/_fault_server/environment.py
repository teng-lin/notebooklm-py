"""Session-level isolation for synthetic local scenarios and their CLI runner."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def isolated_environment() -> Iterator[None]:
    """Isolate once outside concurrent cohorts, preserving the process HOME."""
    previous = {key: value for key, value in os.environ.items() if key.startswith("NOTEBOOKLM_")}
    with tempfile.TemporaryDirectory(prefix="notebooklm-fault-stress-") as directory:
        try:
            for key in previous:
                os.environ.pop(key, None)
            os.environ.update(
                NOTEBOOKLM_HOME=directory,
                NOTEBOOKLM_PROFILE="agent-fault-stress",
                NOTEBOOKLM_DISABLE_KEEPALIVE_POKE="1",
            )
            yield
        finally:
            for key in list(os.environ):
                if key.startswith("NOTEBOOKLM_"):
                    os.environ.pop(key)
            os.environ.update(previous)
