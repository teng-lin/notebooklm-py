"""exactly-once warning dedupe under a single event loop.

These tests pin the **single-event-loop** case of the warning-once contract for
the two canonical owners:

- ``_SECONDARY_BINDING_WARNED`` (canonical owner: ``_auth.cookie_policy``)
- the manager-lifecycle cookie warning claim (owner: ``_auth.storage_lock``)

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
    """Reset cookie policy and isolate the lock-manager warning lifecycle.

    ``conftest.py::_reset_poke_state`` already resets these flags for every
    test in the suite; this local fixture is kept for read-in-isolation
    clarity so a developer scanning ``test_warning_dedupe.py`` alone sees
    the reset alongside the assertions it enables. Writing to
    ``auth_module._SECONDARY_BINDING_WARNED`` would rebind only the auth.py
    re-export captured at import time — after the D1 PR-2 facade retirement
    those names are no longer write-through, so we reset the canonical
    owners directly.
    """
    from notebooklm._auth import cookie_policy as _cookie_policy
    from notebooklm._auth import profile_store as _profile_store
    from notebooklm._auth.storage_lock import StorageLockManager

    _cookie_policy._SECONDARY_BINDING_WARNED = False
    isolated_locks = StorageLockManager()
    original_locks = _profile_store._STORAGE_LOCKS
    _profile_store._STORAGE_LOCKS = isolated_locks
    try:
        yield
    finally:
        _cookie_policy._SECONDARY_BINDING_WARNED = False
        _profile_store._STORAGE_LOCKS = original_locks


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
    # Warning flag now lives on the cookie_policy seam (_AuthFacadeModule
    # retired in D1 PR-2). Read from the owner directly rather than the
    # auth-module re-export captured at import time.
    from notebooklm._auth import cookie_policy as _cookie_policy

    assert _cookie_policy._SECONDARY_BINDING_WARNED is True


def test_flock_unavailable_warns_exactly_once_under_asyncio_gather(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """100 concurrent coroutines hit the flock-unavailable warning path; the
    warning must fire exactly once on a single event loop."""

    @contextlib.contextmanager
    def fake_acquire(request) -> Iterator[object]:  # type: ignore[no-untyped-def]
        # Simulate the manager's lock-infrastructure failure result.
        from notebooklm._auth.storage_lock import LockState

        yield LockState.UNAVAILABLE

    import notebooklm._auth.profile_store as _profile_store

    monkeypatch.setattr(_profile_store._STORAGE_LOCKS, "acquire", fake_acquire)

    lock_path = tmp_path / ".storage_state.json.lock"

    async def first_access() -> None:
        store = _profile_store.ProfileStore(lock_path.with_name("storage_state.json"))
        store._under_blocking_cookie_lock(  # noqa: SLF001
            operation="save_cookies_to_storage",
            body=lambda: None,
        )

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
    # Warning ownership follows the isolated manager lifecycle.
    from notebooklm._auth import profile_store as _profile_store

    assert _profile_store._STORAGE_LOCKS._cookie_warning_claimed is True
