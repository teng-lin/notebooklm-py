"""P8 regressions for legacy ``from_storage`` provider ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from notebooklm._auth import tokens as auth_tokens_module
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.web_provider_storage import WebProviderBootstrap
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


def _auth(path: Path) -> AuthTokens:
    return AuthTokens(
        cookies={("SID", ".google.com", "/"): "cookie"},
        csrf_token="csrf",
        session_id="session",
        storage_path=path,
        cookie_jar=CookieJar.from_domain_map({("SID", ".google.com", "/"): "cookie"}).to_httpx(),
    )


@pytest.mark.asyncio
async def test_file_loaded_custom_client_without_provider_still_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P8 baseline registration remains conditional for custom subclasses."""
    path = tmp_path / "storage_state.json"
    auth = _auth(path)
    store = ProfileStore(path)
    baseline = CookieJar()

    async def load(**_kwargs: Any) -> auth_tokens_module.FileLoadedAuth:
        return auth_tokens_module.FileLoadedAuth(auth, store, baseline)

    monkeypatch.setattr(auth_tokens_module, "_load_stored_auth", load)

    class BareClient(NotebookLMClient):
        def __init__(self, loaded_auth: AuthTokens, **_kwargs: Any) -> None:
            self.seen_auth = loaded_auth

    built = cast(BareClient, await BareClient.from_storage(str(path))._build())
    assert built.seen_auth is auth
    assert not hasattr(built, "_provider")


@pytest.mark.asyncio
async def test_from_storage_context_owns_opened_but_not_awaited_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper closes only the provider of the client it actually entered."""
    import notebooklm.client as client_module

    async def load(**_kwargs: Any) -> WebProviderBootstrap:
        return WebProviderBootstrap(auth=_auth(tmp_path / "storage_state.json"))

    class LocalClient(NotebookLMClient):
        def __init__(self, loaded_auth: AuthTokens, **_kwargs: Any) -> None:
            shell = NotebookLMClient(loaded_auth)
            vars(self).update(vars(shell))

    monkeypatch.setattr(client_module, "load_web_provider_bootstrap", load)

    awaited_context = LocalClient.from_storage()
    with pytest.warns(DeprecationWarning, match="removed in v1.0"):
        awaited = await awaited_context
    awaited_closes = 0
    awaited_close = awaited._provider.close

    async def record_awaited_close() -> None:
        nonlocal awaited_closes
        awaited_closes += 1
        await awaited_close()

    monkeypatch.setattr(awaited._provider, "close", record_awaited_close)
    await awaited_context.__aexit__(None, None, None)
    assert awaited_closes == 0
    await awaited.close()
    assert awaited_closes == 1

    entered_context = LocalClient.from_storage()
    entered_closes = 0
    async with entered_context as entered:
        entered_close = entered._provider.close

        async def record_entered_close() -> None:
            nonlocal entered_closes
            entered_closes += 1
            await entered_close()

        monkeypatch.setattr(entered._provider, "close", record_entered_close)
        assert entered.is_connected is True

    assert entered_closes == 1
    assert entered.is_connected is False
