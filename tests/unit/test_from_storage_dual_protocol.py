"""Tests for the dual-protocol wrapper returned by ``NotebookLMClient.from_storage``.

In v0.5.0 ``from_storage`` stopped being an async coroutine and started
returning ``_FromStorageContext`` — an awaitable async-context-manager
wrapper that supports BOTH:

- ``async with NotebookLMClient.from_storage(...) as client:`` (canonical,
  no ``DeprecationWarning``).
- ``async with await NotebookLMClient.from_storage(...) as client:`` /
  bare ``await NotebookLMClient.from_storage(...)`` (legacy, emits
  ``DeprecationWarning`` naming the v1.0 removal).

This module covers both protocols, the deprecation message text, the
type identity of the yielded value, and the lazy-load behavior that
``_FromStorageContext`` itself does no I/O until used.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from notebooklm.client import NotebookLMClient, _FromStorageContext


def _write_storage_state(tmp_path: Path) -> Path:
    """Drop a minimal storage_state.json on disk and return the path."""
    storage_file = tmp_path / "storage_state.json"
    storage_state = {
        "cookies": [
            {"name": "SID", "value": "dual_sid", "domain": ".google.com"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "dual_1psidts",
                "domain": ".google.com",
            },
            {"name": "HSID", "value": "dual_hsid", "domain": ".google.com"},
        ],
        "origins": [],
    }
    storage_file.write_text(json.dumps(storage_state))
    return storage_file


def _stub_homepage(httpx_mock: HTTPXMock) -> None:
    """Stub the notebooklm homepage GET used to seed the CSRF/session tokens."""
    html = '"SNlM0e":"dual_csrf" "FdrFJe":"dual_session"'
    httpx_mock.add_response(
        url="https://notebooklm.google.com/",
        content=html.encode(),
    )


class TestCanonicalAsyncWith:
    """The new canonical path: ``async with from_storage(...) as client:``."""

    @pytest.mark.asyncio
    async def test_async_with_yields_connected_client(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """No ``await`` on the call; ``__aenter__`` builds + opens the client."""
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        async with NotebookLMClient.from_storage(path=str(storage_file)) as client:
            assert isinstance(client, NotebookLMClient)
            assert client.is_connected is True
            # The auth dropped onto the wrapper survives the lazy build.
            assert client.auth.cookies[("SID", ".google.com", "/")] == "dual_sid"

        # Exiting the context manager tears the session down again.
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_async_with_emits_no_deprecation_warning(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """The canonical path is the migration target — must be warning-free."""
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async with NotebookLMClient.from_storage(path=str(storage_file)) as client:
                _ = client  # nothing — just want a clean entry/exit
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations == [], (
            f"Canonical `async with from_storage(...)` must not emit "
            f"DeprecationWarning; got: {[str(w.message) for w in deprecations]}"
        )


class TestLegacyAwaitForm:
    """The legacy path: ``await from_storage(...)`` / ``async with await ...``."""

    @pytest.mark.asyncio
    async def test_await_still_returns_client(self, tmp_path: Path, httpx_mock: HTTPXMock) -> None:
        """Bare ``await`` returns a built-but-unentered client (legacy contract)."""
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        with pytest.warns(DeprecationWarning, match="removed in v1.0"):
            client = await NotebookLMClient.from_storage(path=str(storage_file))

        assert isinstance(client, NotebookLMClient)
        # ``from_storage`` historically returned an unentered client; the
        # legacy path preserves that contract so existing call sites that
        # then do ``async with client:`` keep working.
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_async_with_await_pipes_through(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """``async with await from_storage(...)`` still works end-to-end."""
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        with pytest.warns(DeprecationWarning, match="removed in v1.0"):
            unentered = await NotebookLMClient.from_storage(path=str(storage_file))
        async with unentered as client:
            assert isinstance(client, NotebookLMClient)
            assert client.is_connected is True
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_deprecation_warning_mentions_v1_removal(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """The warning must name v1.0 so users have a migration target."""
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await NotebookLMClient.from_storage(path=str(storage_file))

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "Expected at least one DeprecationWarning"
        msg = str(deprecations[0].message)
        assert "removed in v1.0" in msg, (
            f"DeprecationWarning must include 'removed in v1.0' for migration clarity; got: {msg!r}"
        )
        # The message should also point users at the new idiom.
        assert "async with" in msg, (
            f"DeprecationWarning should point at the canonical idiom; got: {msg!r}"
        )


class TestWrapperShape:
    """Properties of the wrapper itself, independent of either protocol."""

    def test_from_storage_is_sync(self, tmp_path: Path) -> None:
        """``from_storage`` no longer returns a coroutine — it returns a wrapper.

        Crucially, calling it does NOT await anything: it just constructs
        ``_FromStorageContext`` with the kwargs captured. No I/O happens
        until ``__aenter__`` or ``__await__``.
        """
        # No event loop needed; calling ``from_storage`` is sync.
        wrapper = NotebookLMClient.from_storage(
            path=str(tmp_path / "nonexistent.json"),
        )
        assert isinstance(wrapper, _FromStorageContext)
        # No coroutine, so nothing to await — and importantly, the missing
        # storage file has NOT been read yet (no FileNotFoundError).

    @pytest.mark.asyncio
    async def test_yielded_value_is_notebooklmclient(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """Type-check assertion (runtime): ``async with ... as client`` is a client.

        ``_FromStorageContext.__aenter__`` is typed to return
        ``NotebookLMClient`` so static type-checkers (mypy / pyright)
        narrow the ``as`` binding to ``NotebookLMClient``. We assert the
        runtime side here; mypy enforces the static side via the type
        annotation on ``__aenter__``.
        """
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        async with NotebookLMClient.from_storage(path=str(storage_file)) as client:
            assert isinstance(client, NotebookLMClient)

    @pytest.mark.asyncio
    async def test_build_is_idempotent_when_called_twice(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """Awaiting and then entering the same wrapper must not double-load auth.

        Pathological but legal sequence: ``client = await wrapper`` (legacy
        path returns the unentered client), then ``async with wrapper as
        client2:`` enters via the same wrapper. Both call sites should
        see the same ``NotebookLMClient`` instance, and the auth load
        should run exactly once.
        """
        storage_file = _write_storage_state(tmp_path)
        # Only one homepage hit allowed — proves auth load doesn't re-run.
        _stub_homepage(httpx_mock)

        wrapper = NotebookLMClient.from_storage(path=str(storage_file))

        with pytest.warns(DeprecationWarning, match="removed in v1.0"):
            client_from_await = await wrapper

        async with wrapper as client_from_aenter:
            assert client_from_aenter is client_from_await
            assert client_from_aenter.is_connected is True

    @pytest.mark.asyncio
    async def test_async_with_propagates_body_exceptions(
        self, tmp_path: Path, httpx_mock: HTTPXMock
    ) -> None:
        """Exceptions inside the ``async with`` body must propagate.

        ``_FromStorageContext.__aexit__`` returns ``None`` (falsy), so
        Python's async-with protocol re-raises the body exception. We
        also confirm the underlying client gets torn down so the test
        is asserting end-to-end teardown, not just the wrapper-level
        no-suppress contract.
        """
        storage_file = _write_storage_state(tmp_path)
        _stub_homepage(httpx_mock)

        sentinel: list[NotebookLMClient] = []
        with pytest.raises(ValueError, match="body raised"):
            async with NotebookLMClient.from_storage(path=str(storage_file)) as client:
                sentinel.append(client)
                assert client.is_connected is True
                raise ValueError("body raised")

        # Even though the body raised, ``__aexit__`` on the wrapper still
        # closed the underlying client.
        assert sentinel[0].is_connected is False
