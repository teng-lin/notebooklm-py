"""Tests for auth keepalive poke + __Secure-1PSIDTS rotation (split from tests/unit/test_auth.py for D1 PR-2).

This file owns one concern from the auth subpackage. The original
``tests/unit/test_auth.py`` (4090 LOC) was split into six concern-aligned
files alongside the deletion of ``_AuthFacadeModule``; see ADR-0003
(superseded) and ADR-0007 (test-monkeypatch policy) for the rationale.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm import auth as auth_module
from notebooklm.auth import (
    KEEPALIVE_ROTATE_URL,
    NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV,
    fetch_tokens,
    fetch_tokens_with_domains,
)

_POKE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")
_NOTEBOOKLM_HOMEPAGE_HTML = (
    b'<html><script>window.WIZ_global_data={"SNlM0e":"csrf_ok","FdrFJe":"sess_ok"};</script></html>'
)


def _stale_storage(path: Path, *, age_seconds: float) -> None:
    """Backdate ``path``'s mtime so the L1 rate-limit guard does not skip the poke."""
    target = path.stat().st_mtime - age_seconds
    os.utime(path, (target, target))


class TestIsRecentlyRotated:
    """Direct boundary coverage for ``_is_recently_rotated``."""

    def test_none_path_is_not_recent(self):
        assert auth_module._is_recently_rotated(None) is False

    def test_missing_file_is_not_recent(self, tmp_path):
        assert auth_module._is_recently_rotated(tmp_path / "nope.json") is False

    def test_just_written_file_is_recent(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        assert auth_module._is_recently_rotated(path) is True

    def test_age_just_inside_window_is_recent(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        _stale_storage(path, age_seconds=auth_module._KEEPALIVE_RATE_LIMIT_SECONDS - 1.0)
        assert auth_module._is_recently_rotated(path) is True

    def test_age_just_past_window_is_not_recent(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        _stale_storage(path, age_seconds=auth_module._KEEPALIVE_RATE_LIMIT_SECONDS + 1.0)
        assert auth_module._is_recently_rotated(path) is False

    def test_future_mtime_is_not_recent(self, tmp_path):
        """A future mtime (clock skew, NTP step) must not wedge the guard."""
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        future = path.stat().st_mtime + 3600
        os.utime(path, (future, future))
        assert auth_module._is_recently_rotated(path) is False


class TestPokeConcurrencyThrottling:
    """In-process and cross-process throttling of ``_poke_session``."""

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_concurrent_async_callers_share_single_post(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """``asyncio.gather`` over 10 fresh callers must fire exactly one POST.

        The disk mtime guard alone can't do this — none of the callers have
        written storage_state.json yet, so they all see the same stale mtime.
        The in-process ``asyncio.Lock`` + monotonic timestamp is what dedupes
        them.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        # Backdate so the disk mtime fast path doesn't pre-empt the poke.
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                *(auth_module._poke_session(client, storage_path) for _ in range(10))
            )

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"expected exactly one RotateCookies POST across 10 concurrent callers, "
            f"got {len(poke_requests)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_skips_when_external_process_holds_flock(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """If another process holds the ``.rotate.lock`` flock, skip silently."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        # Simulate an external process holding the flock by making the
        # non-blocking acquire raise the real contention errno. Generic
        # ``OSError`` would be treated as "lock infrastructure unavailable"
        # and fail open instead — this test must mimic actual contention.
        import errno as _errno

        if sys.platform == "win32":
            import msvcrt

            def fail_lock(*_args, **_kwargs):
                raise OSError(_errno.EWOULDBLOCK, "simulated external lock holder")

            monkeypatch.setattr(msvcrt, "locking", fail_lock)
        else:
            import fcntl

            original_flock = fcntl.flock

            def maybe_fail(fd, op):
                if op & fcntl.LOCK_NB:
                    raise OSError(_errno.EWOULDBLOCK, "simulated external lock holder")
                return original_flock(fd, op)

            monkeypatch.setattr(fcntl, "flock", maybe_fail)

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == [], (
            "expected no RotateCookies POST when another process holds the rotation lock"
        )

    def test_rotation_lock_path_is_sibling_of_storage(self, tmp_path):
        """Lock sentinel sits next to the storage file with a ``.rotate.lock`` suffix."""
        storage_path = tmp_path / "storage_state.json"
        lock_path = auth_module._rotation_lock_path(storage_path)
        assert lock_path == tmp_path / ".storage_state.json.rotate.lock"

    def test_rotation_lock_path_returns_none_for_no_storage(self):
        assert auth_module._rotation_lock_path(None) is None

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_flock_released_after_poke(self, tmp_path, httpx_mock: HTTPXMock):
        """A successful poke releases the rotation flock so the next call can acquire."""
        if sys.platform == "win32":
            pytest.skip("POSIX-specific test; Windows uses msvcrt.locking")

        import fcntl

        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)

        # After the poke, an external attempt to acquire LOCK_EX | LOCK_NB
        # should succeed — proving we released our hold.
        lock_path = auth_module._rotation_lock_path(storage_path)
        assert lock_path is not None and lock_path.exists()
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_rotate_cookies_honours_disable_env(self, monkeypatch, httpx_mock: HTTPXMock):
        """``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` short-circuits the bare path too.

        Layer-1 ``_poke_session`` already honoured the env var, but the
        layer-2 keepalive loop bypasses ``_poke_session`` and calls
        ``_rotate_cookies`` directly. Without the env-var check on the bare
        function, setting the variable would silently fail to disable the
        background loop.
        """
        monkeypatch.setenv(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV, "1")

        async with httpx.AsyncClient() as client:
            await auth_module._rotate_cookies(client)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == [], (
            "_rotate_cookies must short-circuit when NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_failed_poke_blocks_in_process_retries_within_window(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """A failed POST must still consume the rate-limit window.

        Otherwise 10 fanned-out callers would each wait the full 15 s timeout
        against a hung accounts.google.com — sequential failure stampede.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=503,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)
            _stale_storage(storage_path, age_seconds=120)
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"failed poke must still bump the in-process attempt timestamp; "
            f"got {len(poke_requests)} POSTs (the second should have skipped)"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_per_profile_timestamp_does_not_cross_profiles(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """A poke against profile A must not suppress profile B for the window.

        Multi-profile setups (``~/.notebooklm/profiles/<name>/storage_state.json``)
        are first-class. With a single global timestamp, a CLI invocation under
        profile ``work`` would silence rotation for profile ``personal`` for
        the next minute.
        """
        profile_a = tmp_path / "a" / "storage_state.json"
        profile_b = tmp_path / "b" / "storage_state.json"
        for path in (profile_a, profile_b):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"cookies": []}))
            _stale_storage(path, age_seconds=120)

        httpx_mock.add_response(url=_POKE_URL_RE, status_code=200, is_reusable=True)

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, profile_a)
            await auth_module._poke_session(client, profile_b)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 2, (
            f"each profile must rotate independently; got {len(poke_requests)} POSTs"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_timestamp_stamped_before_post_completes(self, tmp_path, httpx_mock: HTTPXMock):
        """A layer-1 caller arriving while a layer-2 POST is in flight must skip.

        L2 keepalive calls ``_rotate_cookies`` directly (no async lock); if
        the timestamp were only stamped in a ``finally`` after the await, an
        L1 caller arriving mid-flight would see a stale timestamp and fire
        its own POST. Stamping *before* the await closes that overlap.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        gate = asyncio.Event()
        entered = asyncio.Event()
        post_calls = 0

        async def slow_post(*_args, **_kwargs):
            nonlocal post_calls
            post_calls += 1
            entered.set()
            await gate.wait()
            return httpx.Response(
                200,
                request=httpx.Request("POST", auth_module.KEEPALIVE_ROTATE_URL),
            )

        async with httpx.AsyncClient() as client:
            client.post = slow_post  # type: ignore[method-assign]
            # L2-style: bare ``_rotate_cookies``, no per-profile async lock.
            task_l2 = asyncio.create_task(auth_module._rotate_cookies(client, storage_path))
            # Wait for slow_post to enter via an event rather than a timed
            # poll — busy-waits in the 100s of ms range can flake on loaded
            # CI runners (notably Windows) where the first task switch after
            # ``create_task`` doesn't always land in time.
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            assert post_calls == 1, "L2 task should be parked inside slow_post"
            # L1-style: ``_poke_session`` acquires the per-profile async lock
            # (uncontended because L2 didn't take it) and reads the per-profile
            # timestamp. Claimed early, this short-circuits without a 2nd POST.
            await auth_module._poke_session(client, storage_path)
            assert post_calls == 1, (
                f"L1 fired during L2's in-flight POST; early-stamp broken (post_calls={post_calls})"
            )
            gate.set()
            await task_l2

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_concurrent_rotate_cookies_same_profile_share_single_post(self, tmp_path):
        """Two layer-2-style direct ``_rotate_cookies`` calls on the same profile
        must share a single POST — verifies the atomic check-and-claim, not
        just the layer-1 async lock.

        Uses a gate/entered handshake (same pattern as the sibling
        ``test_l2_in_flight_claim_blocks_l1_short_circuit`` at L310) so the
        first caller is parked mid-POST when the second arrives. The
        previous version used ``asyncio.gather`` + ``httpx_mock`` and could
        still pass if the first call refreshed the timestamp before the
        second reached the claim path — non-deterministically weakening
        the assertion (coderabbit finding on PR #834).
        """
        storage_path = tmp_path / "storage_state.json"
        gate = asyncio.Event()
        entered = asyncio.Event()
        post_calls = 0

        async def slow_post(*_args, **_kwargs):
            nonlocal post_calls
            post_calls += 1
            entered.set()
            await gate.wait()
            return httpx.Response(
                200,
                request=httpx.Request("POST", auth_module.KEEPALIVE_ROTATE_URL),
            )

        async with httpx.AsyncClient() as client:
            client.post = slow_post  # type: ignore[method-assign]
            task1 = asyncio.create_task(auth_module._rotate_cookies(client, storage_path))
            # Wait for the first call to park inside slow_post (deterministic
            # via the entered event — busy-waits flake on loaded CI runners).
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            assert post_calls == 1, "first L2 task should be parked inside slow_post"
            # Second L2 call now arrives while the first is mid-POST. The
            # atomic check-and-claim in ``_try_claim_rotation`` must reject
            # it without firing a second POST.
            await auth_module._rotate_cookies(client, storage_path)
            assert post_calls == 1, (
                f"two L2 callers on the same profile must coordinate via the atomic "
                f"claim; got {post_calls} POSTs"
            )
            gate.set()
            await task1

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_lock_unavailable_fails_open(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """Lock infrastructure failure must NOT permanently suppress rotation.

        On read-only auth dirs, NFS without flock support, or permission
        errors opening the sentinel, rotation should fall through to a
        best-effort POST instead of being silenced for the lifetime of the
        process.
        """
        import errno as _errno

        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        original_open = os.open
        rotate_lock = auth_module._rotation_lock_path(storage_path)

        def selective_open(path, *args, **kwargs):
            if str(path) == str(rotate_lock):
                raise OSError(_errno.EACCES, "simulated read-only auth dir")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", selective_open)
        httpx_mock.add_response(url=_POKE_URL_RE, status_code=200, is_reusable=True)

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"infra failure must fail open and let rotation proceed; got {len(poke_requests)} POSTs"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_in_process_timestamp_blocks_within_window(self, tmp_path, httpx_mock: HTTPXMock):
        """A second call before storage save lands still skips via the monotonic timestamp.

        Storage save happens in the caller (``_fetch_tokens_with_jar``) after
        ``_poke_session`` returns, so two successive direct calls would both
        see stale mtime. The monotonic timestamp inside the async lock catches
        the second one.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)
            # storage_state.json mtime is intentionally NOT refreshed between
            # calls — proving the in-memory timestamp is what gates this.
            _stale_storage(storage_path, age_seconds=120)
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"second poke should skip via monotonic timestamp; got {len(poke_requests)} POSTs"
        )


class TestKeepalivePoke:
    """Tests for the proactive ``accounts.google.com/RotateCookies`` poke."""

    @pytest.mark.asyncio
    async def test_poke_made_by_default(self, httpx_mock: HTTPXMock):
        """Token fetch hits RotateCookies before notebooklm.google.com."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        all_urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert len(poke_requests) == 1, (
            f"expected exactly one RotateCookies request, got: {all_urls}"
        )
        # Order matters per the docstring: the RotateCookies poke must precede
        # the notebooklm.google.com fetch so the rotation runs before the
        # cookie jar is consumed for the homepage GET. Without this assertion
        # a regression that flipped the order would still produce a single
        # poke request and silently pass.
        assert all_urls.index(KEEPALIVE_ROTATE_URL) < all_urls.index(
            "https://notebooklm.google.com/"
        ), f"poke must precede notebooklm homepage fetch; saw order {all_urls}"
        assert str(poke_requests[0].url) == KEEPALIVE_ROTATE_URL
        assert poke_requests[0].method == "POST"

    @pytest.mark.asyncio
    async def test_poke_uses_jspb_body_and_origin(self, httpx_mock: HTTPXMock):
        """Body matches the Chrome jspb sentinel; Origin is the accounts surface."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1
        request = poke_requests[0]
        assert request.content == b'[000,"-0000000000000000000"]'
        assert request.headers.get("content-type") == "application/json"
        assert request.headers.get("origin") == "https://accounts.google.com"

    @pytest.mark.asyncio
    async def test_poke_skipped_when_disabled(self, monkeypatch, httpx_mock: HTTPXMock):
        """``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` suppresses the poke."""
        monkeypatch.setenv(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV, "1")
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == []

    @pytest.mark.asyncio
    async def test_poke_skipped_when_storage_recently_rotated(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """Storage_state.json mtime within the rate-limit window suppresses the poke."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        # storage_state.json was just written — mtime is "now", well inside the 60s window.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == [], (
            "rate-limit guard should skip RotateCookies when storage_state.json is fresh"
        )

    @pytest.mark.asyncio
    async def test_poke_fires_when_storage_older_than_window(self, tmp_path, httpx_mock: HTTPXMock):
        """An older storage_state.json mtime allows the rotation poke through."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        _stale_storage(storage_path, age_seconds=120)
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, "expected RotateCookies poke when storage is stale"

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_token_fetch_succeeds_when_poke_5xx(self, httpx_mock: HTTPXMock):
        """A failing poke is best-effort and never aborts token fetch."""
        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=503,
            is_reusable=True,
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        csrf, session_id = await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_poke_rotated_sidts_lands_in_jar(self, tmp_path, httpx_mock: HTTPXMock):
        """Set-Cookie from RotateCookies response is persisted to storage_state.json."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "SID",
                            "value": "old_sid",
                            "domain": ".google.com",
                            "path": "/",
                        },
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "stale_sidts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        # Backdate so the rate-limit guard doesn't pre-empt the poke.
        _stale_storage(storage_path, age_seconds=120)
        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            headers={
                "Set-Cookie": (
                    "__Secure-1PSIDTS=ROTATED; Domain=.google.com; Path=/; Secure; HttpOnly"
                )
            },
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        rewritten = json.loads(storage_path.read_text())
        sidts_values = [c["value"] for c in rewritten["cookies"] if c["name"] == "__Secure-1PSIDTS"]
        assert sidts_values == ["ROTATED"], (
            f"expected rotated SIDTS persisted to disk, got: {sidts_values}"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_token_fetch_succeeds_when_poke_raises_httperror(self, httpx_mock: HTTPXMock):
        """Network-level HTTPError on the poke is swallowed at DEBUG; token fetch proceeds."""
        httpx_mock.add_exception(httpx.ConnectError("simulated DNS failure"), url=_POKE_URL_RE)
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        csrf, session_id = await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"
