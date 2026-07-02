"""Regression test for the offload save_cookies_to_storage off the loop.

Pre-fix: ``AuthTokens.from_storage`` and
``fetch_tokens_with_domains`` called the *synchronous*
``save_cookies_to_storage`` directly from an ``async`` context. The
function performs file I/O (atomic-replace + fsync + flock); when the
underlying storage is slow (network FS, encrypted home, fcntl
contention with a sibling process), it stalls the whole event loop.

Post-fix: each call site wraps the synchronous save with
``await asyncio.to_thread(save_cookies_to_storage, ...)``, so the
blocking work runs on the default thread executor and the event loop
keeps spinning sibling tasks.

This test monkeypatches ``notebooklm._auth.storage.save_cookies_to_storage`` to
``time.sleep(0.5)``. While ``AuthTokens.from_storage`` is mid-save, a
concurrently scheduled async task increments a counter every 50 ms.
Pre-fix the counter is ~0–1 (loop frozen by the sync sleep); post-fix
it ticks ~10 times (the sleep runs on a thread, loop keeps spinning).

These are unit-style regression tests under ``tests/unit/concurrency/`` so the
blocking-I/O contract is exercised without the integration harness.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from pytest_httpx import HTTPXMock

import notebooklm._auth.refresh as _auth_refresh
import notebooklm._auth.storage as _auth_storage
from notebooklm import auth as auth_module
from notebooklm.auth import AuthTokens

# Time budget for the "blocking sleep" injected into save_cookies_to_storage.
# Half a second is comfortably above the asyncio scheduler resolution but
# short enough to keep the test fast.
_SLEEP_SECONDS = 0.5

# Heartbeat cadence for the sibling task that proves the loop is alive.
# 50 ms gives ~10 ticks in the 0.5 s window. The tick is a bare ``asyncio.sleep``
# (see ``_heartbeat``) so per-tick overhead stays tiny even under coverage
# instrumentation — an earlier ``wait_for(Event.wait())`` tick built a future +
# timer and raised/caught a ``TimeoutError`` every iteration, and coverage.py
# tracing that machinery inflated the period enough to drop the count to 4 on a
# contended macOS CI runner (flaking a >=5 bound).
_HEARTBEAT_INTERVAL = 0.05

# Lower bound on observed heartbeats during the save window. This test is a
# FROZEN-vs-ALIVE discriminator, not a throughput benchmark: pre-fix the
# synchronous sleep blocks the loop for ~0.5 s so the counter stays at 0 or 1
# (one tick may sneak in before the save is entered); post-fix the loop is free
# and ticks ~10 times. 3 sits cleanly above the frozen ceiling (1) with wide
# margin below the healthy count — the number is a floor, not a target.
_MIN_HEARTBEATS = 3


@pytest.mark.asyncio
async def test_from_storage_save_does_not_block_event_loop(
    tmp_path,
    monkeypatch,
    httpx_mock: HTTPXMock,
) -> None:
    """``AuthTokens.from_storage`` must not freeze the loop on save.

    Wraps a ``time.sleep(0.5)`` over the storage save and asserts a
    concurrently scheduled heartbeat keeps ticking during the
    save window — proof that the save runs off the loop (i.e. via
    ``asyncio.to_thread``).
    """
    storage_file = tmp_path / "storage_state.json"
    storage_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "sid", "domain": ".google.com"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "test_1psidts",
                        "domain": ".google.com",
                    },
                ]
            }
        )
    )
    # Match the existing TestAuthTokensFromStorage fixture so the token
    # fetch resolves to a complete AuthTokens (csrf + session_id).
    html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
    httpx_mock.add_response(content=html.encode())

    # Replace the storage save with a synchronous sleep. Returning ``True``
    # mirrors the legacy "ok" return path of save_cookies_to_storage when
    # the caller does not request a structured result. The from_storage
    # caller passes ``return_result=True`` and expects a ``CookieSaveResult``
    # or ``bool``; ``True`` flows the no-snapshot branch (cookie_snapshot
    # is set to ``None``), which is fine for this test's assertions.
    def _blocking_save(*args: object, **kwargs: object) -> bool:
        time.sleep(_SLEEP_SECONDS)
        return True

    # ``AuthTokens.from_storage`` owns token loading in ``_auth.tokens`` and
    # resolves the storage save through the private owner module.
    monkeypatch.setattr(_auth_storage, "save_cookies_to_storage", _blocking_save)

    heartbeats = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        """Increment a counter every _HEARTBEAT_INTERVAL until stopped."""
        nonlocal heartbeats
        while not stop.is_set():
            # Bare sleep, not wait_for(Event) — a cheap tick keeps the count
            # stable under coverage (see _HEARTBEAT_INTERVAL). Shutdown latency is
            # one interval (the awaiting `finally` absorbs it); a frozen loop still
            # can't fire this sleep during the save, so it stays the 0-1 baseline.
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            heartbeats += 1

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        # Give the heartbeat one tick to start, then drive the save path.
        await asyncio.sleep(0)
        tokens = await AuthTokens.from_storage(storage_file)
    finally:
        stop.set()
        await heartbeat_task

    # Sanity: the save path completed successfully.
    assert tokens.csrf_token == "csrf_token"
    assert tokens.session_id == "session_id"

    assert heartbeats >= _MIN_HEARTBEATS, (
        f"Event loop was blocked during save_cookies_to_storage: only "
        f"{heartbeats} heartbeats fired in {_SLEEP_SECONDS}s "
        f"(expected >= {_MIN_HEARTBEATS}). The synchronous save is "
        f"still running on the event-loop thread."
    )


@pytest.mark.asyncio
async def test_fetch_tokens_with_domains_save_does_not_block_event_loop(
    tmp_path,
    monkeypatch,
    httpx_mock: HTTPXMock,
) -> None:
    """``fetch_tokens_with_domains`` in ``notebooklm._auth.refresh`` must offload its save too.

    Same protocol as the from_storage test but exercises the second
    documented call site so a regression that fixes only one of the two
    sites still fails this suite.
    """
    storage_file = tmp_path / "storage_state.json"
    storage_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "sid", "domain": ".google.com"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "test_1psidts",
                        "domain": ".google.com",
                    },
                ]
            }
        )
    )
    html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
    httpx_mock.add_response(content=html.encode())

    def _blocking_save(*args: object, **kwargs: object) -> None:
        # fetch_tokens_with_domains discards the return value, so any
        # return is fine; mirror the real function's None-by-default.
        time.sleep(_SLEEP_SECONDS)

    # ``fetch_tokens_with_domains`` resolves ``save_cookies_to_storage`` through the
    # module-local alias in ``notebooklm._auth.refresh``, so patch the consumer-side
    # name rather than the canonical home in ``_auth.storage``.
    monkeypatch.setattr(_auth_refresh, "save_cookies_to_storage", _blocking_save)

    heartbeats = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal heartbeats
        while not stop.is_set():
            # Bare sleep, not wait_for(Event) — a cheap tick keeps the count
            # stable under coverage (see _HEARTBEAT_INTERVAL). Shutdown latency is
            # one interval (the awaiting `finally` absorbs it); a frozen loop still
            # can't fire this sleep during the save, so it stays the 0-1 baseline.
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            heartbeats += 1

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        await asyncio.sleep(0)
        csrf, session_id = await auth_module.fetch_tokens_with_domains(storage_file)
    finally:
        stop.set()
        await heartbeat_task

    assert csrf == "csrf_token"
    assert session_id == "session_id"

    assert heartbeats >= _MIN_HEARTBEATS, (
        f"Event loop was blocked during fetch_tokens_with_domains save: only "
        f"{heartbeats} heartbeats fired in {_SLEEP_SECONDS}s "
        f"(expected >= {_MIN_HEARTBEATS}). The synchronous save is still "
        f"running on the event-loop thread."
    )
