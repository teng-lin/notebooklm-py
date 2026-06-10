"""Unit tests for the legacy notebook share manager."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._notebooks import NotebooksAPI
from notebooklm._sharing_manager import ShareManager, build_share_url
from notebooklm.rpc import RPCMethod
from tests._fixtures.fake_core import make_fake_core

BASE_URL = "https://notebooklm.google.com"


def _make_rpc() -> AsyncMock:
    return AsyncMock(return_value=None)


def _make_manager() -> tuple[ShareManager, AsyncMock]:
    rpc = _make_rpc()
    core = make_fake_core(rpc_call=rpc)
    return ShareManager(core.rpc_executor, base_url_provider=lambda: BASE_URL), rpc


def test_build_share_url_without_artifact() -> None:
    assert build_share_url(BASE_URL, "nb_123") == "https://notebooklm.google.com/notebook/nb_123"


def test_build_share_url_with_artifact() -> None:
    assert (
        build_share_url(BASE_URL, "nb_123", "art_456")
        == "https://notebooklm.google.com/notebook/nb_123?artifactId=art_456"
    )


def test_build_share_url_quotes_ids() -> None:
    """Reserved characters and whitespace in IDs must be percent-encoded.

    Without ``safe=""`` quoting, an ID like ``"foo bar/baz"`` would slip a
    raw ``/`` into the path position and rewrite the URL into another
    endpoint, and a raw space would produce an invalid URL.
    """
    url = build_share_url(BASE_URL, "foo bar/baz", artifact_id="qux?frag&y")
    assert "foo%20bar%2Fbaz" in url
    # Reserved characters in the artifact id are also encoded so they cannot
    # smuggle additional query params or fragments.
    assert "qux%3Ffrag%26y" in url
    # Sanity: the raw, un-encoded forms must NOT appear anywhere in the URL.
    assert "foo bar/baz" not in url
    assert "qux?frag&y" not in url


@pytest.mark.asyncio
async def test_share_public_with_artifact_sends_legacy_payload_and_returns_deep_link() -> None:
    manager, rpc = _make_manager()

    result = await manager.share("nb_123", public=True, artifact_id="art_456")

    assert result == {
        "public": True,
        "url": "https://notebooklm.google.com/notebook/nb_123?artifactId=art_456",
        "artifact_id": "art_456",
    }
    rpc.assert_awaited_once_with(
        RPCMethod.SHARE_ARTIFACT,
        [[1], "nb_123", "art_456"],
        source_path="/notebook/nb_123",
        allow_null=True,
    )


@pytest.mark.asyncio
async def test_share_public_without_artifact_returns_notebook_url() -> None:
    manager, rpc = _make_manager()

    result = await manager.share("nb_123")

    assert result == {
        "public": True,
        "url": "https://notebooklm.google.com/notebook/nb_123",
        "artifact_id": None,
    }
    rpc.assert_awaited_once_with(
        RPCMethod.SHARE_ARTIFACT,
        [[1], "nb_123"],
        source_path="/notebook/nb_123",
        allow_null=True,
    )


@pytest.mark.asyncio
async def test_share_private_sends_disable_payload_and_returns_no_url() -> None:
    manager, rpc = _make_manager()

    result = await manager.share("nb_123", public=False)

    assert result == {"public": False, "url": None, "artifact_id": None}
    rpc.assert_awaited_once_with(
        RPCMethod.SHARE_ARTIFACT,
        [[0], "nb_123"],
        source_path="/notebook/nb_123",
        allow_null=True,
    )


@pytest.mark.asyncio
async def test_share_private_with_artifact_preserves_artifact_id_but_returns_no_url() -> None:
    manager, rpc = _make_manager()

    result = await manager.share("nb_123", public=False, artifact_id="art_456")

    assert result == {"public": False, "url": None, "artifact_id": "art_456"}
    rpc.assert_awaited_once_with(
        RPCMethod.SHARE_ARTIFACT,
        [[0], "nb_123", "art_456"],
        source_path="/notebook/nb_123",
        allow_null=True,
    )


def test_get_share_url_is_sync_and_does_not_call_rpc() -> None:
    manager, rpc = _make_manager()

    url = manager.get_share_url("nb_123", artifact_id="art_456")

    assert url == "https://notebooklm.google.com/notebook/nb_123?artifactId=art_456"
    rpc.assert_not_called()


@pytest.mark.asyncio
async def test_notebooks_api_default_share_manager_uses_late_bound_rpc_executor_call() -> None:
    """The auto-built ``_share_manager`` late-binds the executor's rpc_call.

    ``NotebooksAPI.share()`` was removed in v0.8.0 (#1363), but the default
    ``ShareManager`` it constructed (still backing ``get_share_url``) keeps the
    late-binding contract: ShareManager binds to the executor's ``rpc_call``
    attribute lazily, so swapping it after construction must be honored. Driven
    directly through ``_share_manager.share`` (the manager stays; only the public
    wrapper was cut).
    """
    core = make_fake_core(rpc_call=AsyncMock(return_value=None))
    api = NotebooksAPI(core.rpc_executor, sources_api=MagicMock())
    replacement_rpc = AsyncMock(return_value=None)
    # ShareManager binds to the executor's rpc_call attribute lazily — swap
    # it to verify the late-binding contract. This is intentional behavior
    # under test, not the forbidden pattern (we're testing the binding).
    core.rpc_executor.rpc_call = replacement_rpc

    result = await api._share_manager.share("nb_123", public=True, artifact_id="art_456")

    assert result["url"] == "https://notebooklm.google.com/notebook/nb_123?artifactId=art_456"
    replacement_rpc.assert_awaited_once_with(
        RPCMethod.SHARE_ARTIFACT,
        [[1], "nb_123", "art_456"],
        source_path="/notebook/nb_123",
        allow_null=True,
    )


def test_notebooks_api_share_method_removed_in_v080() -> None:
    """NotebooksAPI.share() was removed in v0.8.0 (#1363).

    The public wrapper that delegated to the injected ``ShareManager.share`` is
    gone; callers use ``client.sharing.set_public`` (toggle) and
    ``get_share_url`` (deep-link URL). The manager-delegation contract is still
    exercised by ``ShareManager.share`` tests above and ``get_share_url`` below.
    """
    core = MagicMock()
    share_manager = MagicMock()
    api = NotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)

    assert not hasattr(api, "share")


def test_notebooks_api_get_share_url_delegates_to_injected_share_manager() -> None:
    core = MagicMock()
    share_manager = MagicMock()
    share_manager.get_share_url.return_value = "https://example.test/notebook/nb_123"
    api = NotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    share_manager.get_share_url.assert_called_once_with("nb_123", None)
