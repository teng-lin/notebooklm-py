"""exactly-once warning dedupe under a single event loop.

These tests pin the **single-event-loop** case of the documented "best-effort
under threads, exactly-once on a single loop" contract for the two module-level
warning flags in ``notebooklm.auth``:

- ``_SECONDARY_BINDING_WARNED`` (auth.py)
- ``_FLOCK_UNAVAILABLE_WARNED`` (auth.py)

Both follow the same shape: a synchronous ``if not flag: flag = True; warn()``
block with no ``await`` between the check and the set. The asyncio scheduler
only switches coroutines at ``await`` points, so concurrent coroutines on one
loop cannot interleave inside that block — the warning must fire exactly once
even under ``asyncio.gather(*[N coros])``. We assert that here with N=100.

Thread-mode dedupe is intentionally NOT tested — per the documented
concurrency contract (each client is bound to a single event loop), threading
is out of scope and a duplicate warning under threads is accepted as
best-effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from notebooklm import auth as auth_module

# Cookie set that passes the Tier 1 required-cookies check but lacks any
# secondary binding (no OSID, no APISID/SAPISID pair). This triggers the
# ``_SECONDARY_BINDING_WARNED`` warning path in ``_validate_required_cookies``.
_TIER1_ONLY_COOKIES = {"SID", "__Secure-1PSIDTS"}


@pytest.fixture(autouse=True)
def _reset_warning_flags() -> Iterator[None]:
    """Reset both module-level warning flags around each test."""
    auth_module._SECONDARY_BINDING_WARNED = False
    auth_module._FLOCK_UNAVAILABLE_WARNED = False
    try:
        yield
    finally:
        auth_module._SECONDARY_BINDING_WARNED = False
        auth_module._FLOCK_UNAVAILABLE_WARNED = False


def test_secondary_binding_warns_exactly_once_under_asyncio_gather(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """100 concurrent coroutines hit the secondary-binding warning path; the
    warning must fire exactly once on a single event loop."""

    async def first_access() -> None:
        # ``_validate_required_cookies`` is synchronous, but invoking it from
        # inside a coroutine is the realistic shape (it's called from the
        # async cookie loaders). The asyncio scheduler can only switch tasks
        # at ``await`` points; since the check-and-set is a single sync block,
        # gathered tasks cannot interleave inside it.
        auth_module._validate_required_cookies(_TIER1_ONLY_COOKIES)

    async def run_gather() -> None:
        await asyncio.gather(*(first_access() for _ in range(100)))

    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        asyncio.run(run_gather())

    binding_warnings = [rec for rec in caplog.records if "secondary binding" in rec.getMessage()]
    assert len(binding_warnings) == 1, (
        f"expected exactly one secondary-binding warning, "
        f"got {len(binding_warnings)}: {[r.getMessage() for r in binding_warnings]}"
    )
    assert auth_module._SECONDARY_BINDING_WARNED is True


def test_flock_unavailable_warns_exactly_once_under_asyncio_gather(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """100 concurrent coroutines hit the flock-unavailable warning path; the
    warning must fire exactly once on a single event loop."""

    @contextlib.contextmanager
    def fake_file_lock(lock_path: Path, *, blocking: bool, log_prefix: str) -> Iterator[str]:
        # Simulate the "lock infrastructure failed" branch — the inner
        # ``_file_lock`` would yield ``"unavailable"`` on NFS without flock
        # support, fd exhaustion, etc. ``_file_lock_exclusive`` is the only
        # caller that emits the dedupe warning, and only on this state.
        yield "unavailable"

    monkeypatch.setattr(auth_module, "_file_lock", fake_file_lock)

    lock_path = tmp_path / ".storage_state.json.lock"

    async def first_access() -> None:
        # ``_file_lock_exclusive`` is a sync context manager; entering and
        # exiting it inside a coroutine mirrors the realistic call shape
        # (it's invoked from the cookie-save path).
        with auth_module._file_lock_exclusive(lock_path):
            pass

    async def run_gather() -> None:
        await asyncio.gather(*(first_access() for _ in range(100)))

    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        asyncio.run(run_gather())

    flock_warnings = [
        rec for rec in caplog.records if "Cross-process file lock unavailable" in rec.getMessage()
    ]
    assert len(flock_warnings) == 1, (
        f"expected exactly one flock-unavailable warning, "
        f"got {len(flock_warnings)}: {[r.getMessage() for r in flock_warnings]}"
    )
    assert auth_module._FLOCK_UNAVAILABLE_WARNED is True
