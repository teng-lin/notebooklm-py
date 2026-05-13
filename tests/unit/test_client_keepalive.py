"""Tests for the NotebookLMClient periodic keepalive task."""

import asyncio
import json
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

ROTATE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")


@pytest.fixture
def mock_auth():
    """Create AuthTokens with no storage path (default tests don't persist)."""
    return AuthTokens(
        cookies={"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts", "HSID": "test_hsid"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


def _storage_auth(tmp_path) -> tuple[AuthTokens, "object"]:
    """Build AuthTokens backed by a real storage_state.json under tmp_path."""
    storage_path = tmp_path / "storage_state.json"
    storage_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "initial_sid",
                        "domain": ".google.com",
                        "path": "/",
                    },
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
    auth = AuthTokens(
        cookies={"SID": "initial_sid", "__Secure-1PSIDTS": "test_1psidts"},
        csrf_token="test_csrf",
        session_id="test_session",
        storage_path=storage_path,
    )
    return auth, storage_path


class TestKeepaliveDisabledByDefault:
    @pytest.mark.asyncio
    async def test_keepalive_off_by_default(self, mock_auth, httpx_mock: HTTPXMock):
        """No keepalive task is spawned and no extra HTTP calls fire by default."""
        client = NotebookLMClient(mock_auth)
        async with client:
            assert client._core._keepalive_task is None
            # Give the loop a chance to run; nothing should happen
            await asyncio.sleep(0.1)

        # No RotateCookies request should have been issued
        for req in httpx_mock.get_requests():
            assert "RotateCookies" not in str(req.url), f"Unexpected keepalive request: {req.url}"


class TestKeepaliveLifecycle:
    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_spawns_task_on_enter_cancels_on_exit(self, mock_auth, httpx_mock: HTTPXMock):
        """Task is created on __aenter__ and cleanly cancelled on __aexit__."""
        httpx_mock.add_response(
            url=ROTATE_URL_RE,
            is_optional=True,
            is_reusable=True,
            status_code=204,
        )

        client = NotebookLMClient(
            mock_auth,
            keepalive=0.05,
            keepalive_min_interval=0.01,
        )

        async with client:
            task = client._core._keepalive_task
            assert task is not None
            assert not task.done()

        # Task should be cleaned up; no warnings should be raised.
        assert client._core._keepalive_task is None
        # Either cancelled or finished; never left dangling.
        assert task.done()


class TestKeepaliveFloor:
    @pytest.mark.asyncio
    async def test_floor_clamps_low_interval(self, mock_auth):
        """Passing keepalive below keepalive_min_interval is clamped up."""
        client = NotebookLMClient(
            mock_auth,
            keepalive=10.0,
            keepalive_min_interval=60.0,
        )
        assert client._core._keepalive_interval == 60.0

    @pytest.mark.asyncio
    async def test_floor_does_not_lower_higher_interval(self, mock_auth):
        """Larger keepalive values pass through unchanged."""
        client = NotebookLMClient(
            mock_auth,
            keepalive=600.0,
            keepalive_min_interval=60.0,
        )
        assert client._core._keepalive_interval == 600.0

    @pytest.mark.asyncio
    async def test_none_keeps_disabled(self, mock_auth):
        """``keepalive=None`` keeps the loop disabled regardless of floor."""
        client = NotebookLMClient(
            mock_auth,
            keepalive=None,
            keepalive_min_interval=60.0,
        )
        assert client._core._keepalive_interval is None


class TestKeepaliveValidation:
    @pytest.mark.parametrize("bad", [0, 0.0, -1, -0.5, float("nan"), float("inf")])
    def test_rejects_non_positive_or_non_finite_keepalive(self, mock_auth, bad):
        """``keepalive`` must be ``None`` or a positive finite number."""
        with pytest.raises(ValueError, match="keepalive"):
            NotebookLMClient(mock_auth, keepalive=bad)

    @pytest.mark.parametrize("bad", [0, 0.0, -1, -0.5, float("nan"), float("inf")])
    def test_rejects_non_positive_or_non_finite_floor(self, mock_auth, bad):
        """``keepalive_min_interval`` must be a positive finite number."""
        with pytest.raises(ValueError, match="keepalive_min_interval"):
            NotebookLMClient(mock_auth, keepalive=120.0, keepalive_min_interval=bad)


class TestKeepalivePokes:
    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_pokes_at_interval(self, mock_auth, httpx_mock: HTTPXMock, monkeypatch):
        """At least two RotateCookies pokes fire within a short window.

        The test uses sub-second intervals to keep wall-clock cheap; the
        in-process rate-limit window (60 s in production) would otherwise
        suppress every iteration past the first. Patch the window down so
        the loop's pacing is the only thing being tested here.
        """
        from notebooklm import auth as _auth

        monkeypatch.setattr(_auth, "_KEEPALIVE_RATE_LIMIT_SECONDS", 0.0)
        httpx_mock.add_response(
            url=ROTATE_URL_RE,
            is_optional=True,
            is_reusable=True,
            status_code=204,
        )

        client = NotebookLMClient(
            mock_auth,
            keepalive=0.05,
            keepalive_min_interval=0.01,
        )

        async with client:
            await asyncio.sleep(0.5)

        poke_requests = [r for r in httpx_mock.get_requests() if "RotateCookies" in str(r.url)]
        assert len(poke_requests) >= 2, (
            f"Expected at least 2 keepalive pokes, got {len(poke_requests)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_failure_does_not_crash_loop(self, mock_auth, httpx_mock: HTTPXMock, monkeypatch):
        """A failing poke is swallowed and the loop continues.

        Same rate-limit-window patch as ``test_pokes_at_interval``: the
        sub-second test interval would otherwise be debounced into a single
        attempt by the in-process claim.
        """
        from notebooklm import auth as _auth

        monkeypatch.setattr(_auth, "_KEEPALIVE_RATE_LIMIT_SECONDS", 0.0)
        # First poke: connection error. Subsequent pokes: 204.
        httpx_mock.add_exception(
            url=ROTATE_URL_RE,
            exception=httpx.ConnectError("simulated network blip"),
        )
        httpx_mock.add_response(
            url=ROTATE_URL_RE,
            is_optional=True,
            is_reusable=True,
            status_code=204,
        )

        client = NotebookLMClient(
            mock_auth,
            keepalive=0.05,
            keepalive_min_interval=0.01,
        )

        async with client:
            await asyncio.sleep(0.4)
            # Task is still running after the failure
            assert client._core._keepalive_task is not None
            assert not client._core._keepalive_task.done()

        poke_requests = [r for r in httpx_mock.get_requests() if "RotateCookies" in str(r.url)]
        # First call raised; at least one further successful call must follow.
        assert len(poke_requests) >= 2, (
            f"Loop should have retried after failure; got {len(poke_requests)} pokes"
        )


class TestKeepalivePersistenceFailure:
    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_persistence_failure_logs_warning_and_continues(
        self, tmp_path, httpx_mock: HTTPXMock, monkeypatch, caplog
    ):
        """A failing ``save_cookies_to_storage`` is logged at WARNING; loop continues."""
        auth, storage_path = _storage_auth(tmp_path)

        httpx_mock.add_response(
            url=ROTATE_URL_RE,
            is_optional=True,
            is_reusable=True,
            status_code=204,
        )

        save_calls = []

        def boom(cookies, path, **kwargs):
            save_calls.append(path)
            raise OSError("simulated disk full")

        monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", boom)

        client = NotebookLMClient(
            auth,
            keepalive=0.05,
            keepalive_min_interval=0.01,
        )

        with caplog.at_level("WARNING", logger="notebooklm._core"):
            async with client:
                # Wait for at least 2 save attempts
                for _ in range(50):
                    if len(save_calls) >= 2:
                        break
                    await asyncio.sleep(0.05)

        assert len(save_calls) >= 2, "Loop should retry after a persistence failure"
        warnings = [
            r for r in caplog.records if r.levelname == "WARNING" and "persistence" in r.message
        ]
        assert warnings, "A persistence failure should surface as a WARNING log record"
        assert str(storage_path) in warnings[0].message


class TestKeepaliveExplicitStoragePath:
    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_explicit_storage_path_used_when_auth_lacks_one(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """``NotebookLMClient(storage_path=...)`` is honored even when ``auth.storage_path`` is ``None``."""
        # storage_state.json on disk, but auth.storage_path stays None
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "SID",
                            "value": "manual_sid",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        auth = AuthTokens(
            cookies={"SID": "manual_sid"},
            csrf_token="t",
            session_id="s",
            # NOTE: storage_path is *not* set on auth
        )

        httpx_mock.add_response(
            url=ROTATE_URL_RE,
            is_optional=True,
            is_reusable=True,
            status_code=204,
            headers={
                "Set-Cookie": (
                    "__Secure-1PSIDTS=rotated_via_explicit; Domain=.google.com; Path=/; Secure"
                ),
            },
        )

        client = NotebookLMClient(
            auth,
            storage_path=storage_path,
            keepalive=0.05,
            keepalive_min_interval=0.01,
        )

        async with client:
            for _ in range(50):
                if "rotated_via_explicit" in storage_path.read_text():
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Rotated cookie was not persisted to the explicit storage path")

    def test_explicit_storage_path_normalizes_onto_auth_without_mutating_caller(self, tmp_path):
        """The constructor exposes ``storage_path`` on ``client.auth`` so
        ``refresh_auth()`` and ``ClientCore.close()`` (which read
        ``self._core.auth.storage_path`` directly, not the keepalive-specific
        path) persist to the same file. Crucially, the caller's original
        ``AuthTokens`` is *not* mutated, so reusing one ``AuthTokens`` across
        multiple ``NotebookLMClient`` instances with different storage paths
        is safe.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text('{"cookies": []}')
        auth = AuthTokens(
            cookies={"SID": "x", "__Secure-1PSIDTS": "test_1psidts"},
            csrf_token="t",
            session_id="s",
            # storage_path intentionally None
        )
        assert auth.storage_path is None

        client = NotebookLMClient(auth, storage_path=storage_path)

        assert client.auth.storage_path == storage_path, (
            "Explicit storage_path must be reflected on client.auth so non-keepalive "
            "code paths (refresh_auth, ClientCore.close) see the same file"
        )
        assert auth.storage_path is None, (
            "Caller's AuthTokens must not be mutated — sharing one AuthTokens "
            "across clients with different storage paths must not leak between them"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_close_persists_to_explicit_storage_path(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """``ClientCore.close()`` calls ``save_cookies_to_storage`` with the
        explicit constructor ``storage_path`` even when keepalive never ran
        and ``auth.storage_path`` was ``None`` originally — proving the
        normalization actually wires the on-close save, not just the
        keepalive loop.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text('{"cookies": []}')
        auth = AuthTokens(
            cookies={"SID": "x", "__Secure-1PSIDTS": "test_1psidts"},
            csrf_token="t",
            session_id="s",
            # storage_path intentionally None
        )

        save_calls: list[tuple[object, object]] = []

        def spy(cookies, path, **kwargs):
            save_calls.append((cookies, path))

        monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", spy)

        client = NotebookLMClient(auth, storage_path=storage_path)
        async with client:
            pass  # no RPC calls; keepalive disabled by default

        # close()'s on-close save must have fired with the explicit storage_path
        assert any(call[1] == storage_path for call in save_calls), (
            f"Expected close() to persist to {storage_path}, "
            f"but got: {[(type(c[0]).__name__, c[1]) for c in save_calls]}"
        )


class TestKeepalivePersistence:
    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_persists_rotated_cookies_without_aexit(self, tmp_path, httpx_mock: HTTPXMock):
        """Rotated Set-Cookie from a poke is written to storage between pokes."""
        auth, storage_path = _storage_auth(tmp_path)

        # The poke response sets a rotated 1PSIDTS on .google.com
        httpx_mock.add_response(
            url=ROTATE_URL_RE,
            is_optional=True,
            is_reusable=True,
            status_code=204,
            headers={
                "Set-Cookie": (
                    "__Secure-1PSIDTS=rotated_value_xyz; Domain=.google.com; Path=/; Secure"
                ),
            },
        )

        client = NotebookLMClient(
            auth,
            keepalive=0.05,
            keepalive_min_interval=0.01,
        )

        async with client:
            # Wait long enough for at least one poke + persist
            for _ in range(50):
                if "rotated_value_xyz" in storage_path.read_text():
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail(
                    "Rotated cookie was not persisted to storage_state.json before __aexit__ ran"
                )

            # Sanity: the rotated cookie was written *while* the client was open,
            # not just at close time.
            data = json.loads(storage_path.read_text())
            cookie_names = {c["name"] for c in data["cookies"]}
            assert "__Secure-1PSIDTS" in cookie_names


class TestSaveCookiesUnification:
    """Tests for ClientCore.save_cookies — the single chokepoint that close,
    keepalive, and refresh_auth all route through."""

    @pytest.mark.asyncio
    async def test_save_cookies_takes_in_process_lock_before_writing(self, tmp_path, monkeypatch):
        """``ClientCore.save_cookies`` holds ``_save_lock`` for the duration of
        the worker-thread write, so an older snapshot can't clobber a newer one
        within the same process."""
        from notebooklm._core import ClientCore

        auth = AuthTokens(
            cookies={"SID": "x", "__Secure-1PSIDTS": "test_1psidts"},
            csrf_token="t",
            session_id="s",
            storage_path=tmp_path / "storage_state.json",
        )
        (tmp_path / "storage_state.json").write_text('{"cookies": []}')
        core = ClientCore(auth)

        lock_held_during_save: list[bool] = []
        call_kwargs: list[dict] = []

        def spy(jar, path, **kwargs):
            """Record lock state and the kwargs.

            Capturing ``kwargs`` here is the regression guard for the §3.4.1
            fix: if ``ClientCore.save_cookies`` ever stops threading
            ``original_snapshot=`` through, the assertion below catches it
            before production silently reverts to legacy merge.
            """
            lock_held_during_save.append(core._save_lock.locked())
            call_kwargs.append(kwargs)
            return True

        monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", spy)

        await core.save_cookies(httpx.Cookies())

        assert lock_held_during_save == [True], (
            "save_cookies must hold _save_lock for the duration of "
            "save_cookies_to_storage so newer state always wins"
        )
        assert len(call_kwargs) == 1 and "original_snapshot" in call_kwargs[0], (
            "save_cookies must forward original_snapshot= to save_cookies_to_storage "
            "(dropping the kwarg would silently revert to legacy full-merge)"
        )

    @pytest.mark.asyncio
    async def test_refresh_auth_routes_save_through_save_cookies(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """``refresh_auth`` no longer calls ``save_cookies_to_storage`` directly;
        it routes through ``ClientCore.save_cookies`` so the in-process lock is
        held — preventing an older keepalive snapshot from clobbering the
        freshly-refreshed tokens."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "SID",
                            "value": "x",
                            "domain": ".google.com",
                            "path": "/",
                        },
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
        auth = AuthTokens(
            cookies={"SID": "x", "__Secure-1PSIDTS": "test_1psidts", "HSID": "y"},
            csrf_token="old_csrf",
            session_id="old_session",
            storage_path=storage_path,
        )

        # NotebookLM homepage with new tokens (refresh_auth scrapes these)
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=b'<html><script>window.WIZ_global_data={"SNlM0e":"new_csrf","FdrFJe":"new_sid"};</script></html>',
        )

        client = NotebookLMClient(auth)

        save_calls: list[bool] = []
        snapshot_kwarg_present: list[bool] = []

        def spy(jar, path, **kwargs):
            """Record whether ``_save_lock`` is held when refresh_auth's save fires
            and whether ``original_snapshot=`` was forwarded.

            The snapshot-kwarg check is the §3.4.1 regression guard: refresh_auth
            must route through the snapshot/delta path, never the legacy
            full-merge path.
            """
            save_calls.append(client._core._save_lock.locked())
            snapshot_kwarg_present.append("original_snapshot" in kwargs)
            return True

        monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", spy)

        async with client:
            await client.refresh_auth()

        # At least one save (refresh_auth) plus close()'s on-close save.
        assert len(save_calls) >= 1
        assert all(save_calls), (
            "Every save fired during refresh_auth + close must be under the "
            f"in-process lock; got {save_calls}"
        )
        assert all(snapshot_kwarg_present), (
            "Every save fired during refresh_auth + close must forward "
            "original_snapshot= so the snapshot/delta path is taken, never "
            "the legacy full-merge fallback"
        )


class TestCrossProcessFileLock:
    """Tests for the OS-level file lock inside save_cookies_to_storage that
    serializes writes from different Python processes (e.g. an in-process
    keepalive plus a cron-driven `notebooklm auth refresh`)."""

    def test_save_cookies_to_storage_acquires_file_lock(self, tmp_path, monkeypatch):
        """``save_cookies_to_storage`` calls ``fcntl.flock(LOCK_EX)`` (POSIX)
        before reading or writing the storage file."""
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX-specific test; Windows uses msvcrt.locking")

        import fcntl

        from notebooklm.auth import save_cookies_to_storage

        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            '{"cookies": [{"name": "SID", "value": "old", "domain": ".google.com", "path": "/"}]}'
        )

        flock_calls: list[int] = []
        original_flock = fcntl.flock

        def spy_flock(fd, op):
            """Record each ``fcntl.flock`` operation while still performing it."""
            flock_calls.append(op)
            return original_flock(fd, op)

        monkeypatch.setattr("fcntl.flock", spy_flock)

        from notebooklm.auth import snapshot_cookie_jar

        jar = httpx.Cookies()
        empty_snapshot = snapshot_cookie_jar(jar)
        jar.set("SID", "new", domain=".google.com", path="/")

        save_cookies_to_storage(jar, storage_path, original_snapshot=empty_snapshot)

        assert fcntl.LOCK_EX in flock_calls, (
            f"Expected an LOCK_EX call before the save, got: {flock_calls}"
        )
        assert fcntl.LOCK_UN in flock_calls, (
            f"Expected an LOCK_UN call after the save, got: {flock_calls}"
        )

    def test_save_cookies_to_storage_creates_lock_sentinel(self, tmp_path):
        """The lock file is a sibling of the storage file with a `.lock` suffix
        so the storage file itself is free for the atomic temp-rename."""
        from notebooklm.auth import save_cookies_to_storage, snapshot_cookie_jar

        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            '{"cookies": [{"name": "SID", "value": "old", "domain": ".google.com", "path": "/"}]}'
        )

        jar = httpx.Cookies()
        empty_snapshot = snapshot_cookie_jar(jar)
        jar.set("SID", "new", domain=".google.com", path="/")

        save_cookies_to_storage(jar, storage_path, original_snapshot=empty_snapshot)

        lock_path = storage_path.with_name(f".{storage_path.name}.lock")
        assert lock_path.exists(), f"Expected lock sentinel at {lock_path}"
