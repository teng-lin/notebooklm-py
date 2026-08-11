"""Regression tests for offloading cookie persistence from the event loop.

Pre-fix: ``AuthTokens.from_storage`` and
``fetch_tokens_with_domains`` called the *synchronous*
``save_cookies_to_storage`` directly from an ``async`` context. The
function performs file I/O (atomic-replace + fsync + flock); when the
underlying storage is slow (network FS, encrypted home, fcntl
contention with a sibling process), it stalls the whole event loop.

Post-fix: stored-auth loading and domain-token refresh both dispatch their
typed ``ProfileStore`` merge to the default thread executor, so blocking work
does not stop sibling tasks.

These tests wrap ``ProfileStore.merge_cookie_observation`` with
``time.sleep(0.5)``. While persistence is in progress, a
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

import notebooklm._auth.cookies as _auth_cookies
import notebooklm._auth.psidts_recovery as _auth_psidts_recovery
import notebooklm._auth.tokens as _auth_tokens
from notebooklm import auth as auth_module
from notebooklm._auth.cookie_policy import RequiredCookieValidationError
from notebooklm._auth.profile_store import ProfileStore
from notebooklm.auth import AuthTokens

# A ``__Secure-1PSIDTS`` scoped to an app host is present by name but does NOT
# route to ``accounts.google.com/RotateCookies`` — the exact condition the
# inline recovery POST exists to heal. The pure loader (require_routable=True)
# rejects it with ``reason="psidts_unroutable"``; the name-only retry accepts it.
_RECOVERABLE_UNROUTABLE_COOKIES = [
    {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
    {"name": "APISID", "value": "apisid", "domain": ".google.com", "path": "/"},
    {"name": "SAPISID", "value": "sapisid", "domain": ".google.com", "path": "/"},
    {
        "name": "__Secure-1PSIDTS",
        "value": "unroutable",
        "domain": "notebooklm.google.com",
        "path": "/",
    },
]

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

    real_merge = ProfileStore.merge_cookie_observation

    def _blocking_merge(self, *args: object, **kwargs: object):
        time.sleep(_SLEEP_SECONDS)
        return real_merge(self, *args, **kwargs)

    # ``AuthTokens.from_storage`` owns token loading in ``_auth.tokens`` and
    # resolves the storage save through the private owner module.
    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", _blocking_merge)

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

    real_merge = ProfileStore.merge_cookie_observation

    def _blocking_merge(self, *args: object, **kwargs: object):
        time.sleep(_SLEEP_SECONDS)
        return real_merge(self, *args, **kwargs)

    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", _blocking_merge)

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
        f"Event loop was blocked during fetch_tokens_with_domains merge: only "
        f"{heartbeats} heartbeats fired in {_SLEEP_SECONDS}s "
        f"(expected >= {_MIN_HEARTBEATS}). The synchronous save is still "
        f"running on the event-loop thread."
    )


@pytest.mark.asyncio
async def test_from_storage_load_recovery_does_not_block_event_loop(
    tmp_path,
    monkeypatch,
    httpx_mock: HTTPXMock,
) -> None:
    """``AuthTokens.from_storage`` must not freeze the loop on inline recovery.

    Regression for the LOAD path (refresh-1 / HIGH#2). Pre-fix
    ``build_httpx_cookies_from_storage`` — and thus its inline
    ``__Secure-1PSIDTS`` recovery — ran synchronously on the event-loop thread
    from ``from_storage``. When the ``RotateCookies`` POST is slow (up to 15 s
    in the wild) the whole loop froze, sometimes while the refresh lock was
    held.

    Post-fix ``from_storage`` offloads the public loader wrapper with
    ``await asyncio.to_thread(...)``, so the blocking recovery runs off the loop
    and sibling tasks keep spinning. We stand in a ``time.sleep`` for the slow
    POST by patching ``_recover_psidts_inline`` (its network + disk write are
    exactly what the offload must move off the loop) and assert a concurrent
    heartbeat keeps ticking during the recovery window.
    """
    storage_file = tmp_path / "storage_state.json"
    storage_file.write_text(json.dumps({"cookies": _RECOVERABLE_UNROUTABLE_COOKIES}))
    html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
    httpx_mock.add_response(content=html.encode())

    def _slow_recovery(_path: object) -> bool:
        # Stand-in for the synchronous 15s RotateCookies POST + fsync'd write.
        # Declining (False) routes the wrapper to the name-only retry, which
        # succeeds because PSIDTS is present by name — so from_storage completes.
        time.sleep(_SLEEP_SECONDS)
        return False

    # ``build_httpx_cookies_from_storage`` reaches recovery via
    # ``from . import psidts_recovery``; patch the attribute on that module.
    monkeypatch.setattr(_auth_psidts_recovery, "_recover_psidts_inline", _slow_recovery)

    heartbeats = 0
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        nonlocal heartbeats
        while not stop.is_set():
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            heartbeats += 1

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        await asyncio.sleep(0)
        tokens = await AuthTokens.from_storage(storage_file)
    finally:
        stop.set()
        await heartbeat_task

    # Sanity: the load path recovered (declined -> name-only) and completed.
    assert tokens.csrf_token == "csrf_token"
    assert tokens.session_id == "session_id"

    assert heartbeats >= _MIN_HEARTBEATS, (
        f"Event loop was blocked during inline PSIDTS recovery on load: only "
        f"{heartbeats} heartbeats fired in {_SLEEP_SECONDS}s "
        f"(expected >= {_MIN_HEARTBEATS}). The synchronous recovery POST is "
        f"still running on the event-loop thread."
    )


class TestPureLoaderPerformsNoNetwork:
    """The inner PURE loaders must NEVER touch the network under any input.

    Recovery (the ``RotateCookies`` POST) is composed only in the public
    wrapper bodies. The pure loaders raise a typed, closed-enum reason and stop;
    a POST from the pure layer would reintroduce the event-loop-blocking defect
    on any caller that forgot to offload. We inject a hard-failing recovery seam
    plus an ``httpx_mock`` with no registered responses (which raises on any
    request) and confirm neither is ever reached.
    """

    @pytest.fixture(autouse=True)
    def _forbid_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _must_not_run(_path: object) -> bool:
            raise AssertionError("pure loader invoked recovery — it must be network-free")

        monkeypatch.setattr(_auth_psidts_recovery, "_recover_psidts_inline", _must_not_run)

    def _write(self, tmp_path, cookies: list[dict]) -> object:
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps({"cookies": cookies}))
        return storage_file

    def test_jar_pure_loader_raises_psidts_unroutable_without_network(
        self, tmp_path, httpx_mock: HTTPXMock
    ) -> None:
        storage_file = self._write(tmp_path, _RECOVERABLE_UNROUTABLE_COOKIES)

        with pytest.raises(RequiredCookieValidationError) as excinfo:
            _auth_cookies._load_cookies_pure(storage_file, require_routable=True)

        assert excinfo.value.reason == "psidts_unroutable"
        assert httpx_mock.get_requests() == []

    def test_flat_pure_loader_raises_psidts_unroutable_without_network(
        self, tmp_path, httpx_mock: HTTPXMock
    ) -> None:
        storage_file = self._write(tmp_path, _RECOVERABLE_UNROUTABLE_COOKIES)

        with pytest.raises(RequiredCookieValidationError) as excinfo:
            _auth_tokens._load_auth_cookies_pure(storage_file, require_routable=True)

        assert excinfo.value.reason == "psidts_unroutable"
        assert httpx_mock.get_requests() == []

    def test_pure_loader_raises_missing_cookie_without_network(
        self, tmp_path, httpx_mock: HTTPXMock
    ) -> None:
        # SID absent: a genuinely broken session no POST can heal.
        cookies = [c for c in _RECOVERABLE_UNROUTABLE_COOKIES if c["name"] != "SID"]
        storage_file = self._write(tmp_path, cookies)

        with pytest.raises(RequiredCookieValidationError) as excinfo:
            _auth_cookies._load_cookies_pure(storage_file, require_routable=True)

        assert excinfo.value.reason == "missing_cookie"
        assert httpx_mock.get_requests() == []

    def test_name_only_pure_loader_accepts_unroutable_without_network(
        self, tmp_path, httpx_mock: HTTPXMock
    ) -> None:
        # The name-only pass (require_routable=False) is the wrapper's post-
        # decline retry: it must succeed on a present-but-unroutable PSIDTS and
        # still never touch the network.
        storage_file = self._write(tmp_path, _RECOVERABLE_UNROUTABLE_COOKIES)

        jar = _auth_cookies._load_cookies_pure(storage_file, require_routable=False)

        assert "__Secure-1PSIDTS" in {c.name for c in jar.jar}
        assert httpx_mock.get_requests() == []
