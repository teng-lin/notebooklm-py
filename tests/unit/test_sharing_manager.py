"""Unit tests for the legacy notebook share manager."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from _fixtures.fake_core import make_fake_core
from notebooklm._notebooks import NotebooksAPI
from notebooklm._sharing_manager import ShareManager, build_share_url
from notebooklm.rpc import RPCMethod

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
    core = make_fake_core(rpc_call=AsyncMock(return_value=None))
    api = NotebooksAPI(core.rpc_executor, sources_api=MagicMock())
    replacement_rpc = AsyncMock(return_value=None)
    # ShareManager binds to the executor's rpc_call attribute lazily — swap
    # it to verify the late-binding contract. This is intentional behavior
    # under test, not the forbidden pattern (we're testing the binding).
    core.rpc_executor.rpc_call = replacement_rpc

    with pytest.warns(DeprecationWarning, match="NotebooksAPI.share"):
        result = await api.share("nb_123", public=True, artifact_id="art_456")

    assert result["url"] == "https://notebooklm.google.com/notebook/nb_123?artifactId=art_456"
    replacement_rpc.assert_awaited_once_with(
        RPCMethod.SHARE_ARTIFACT,
        [[1], "nb_123", "art_456"],
        source_path="/notebook/nb_123",
        allow_null=True,
    )


@pytest.mark.asyncio
async def test_notebooks_api_share_delegates_to_injected_share_manager() -> None:
    core = MagicMock()
    share_manager = MagicMock()
    share_manager.share = AsyncMock(return_value={"public": True, "url": "u", "artifact_id": None})
    api = NotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)

    with pytest.warns(DeprecationWarning, match="NotebooksAPI.share"):
        result = await api.share("nb_123", public=True)

    assert result == {"public": True, "url": "u", "artifact_id": None}
    share_manager.share.assert_awaited_once_with("nb_123", True, None)


def test_notebooks_api_get_share_url_delegates_to_injected_share_manager() -> None:
    core = MagicMock()
    share_manager = MagicMock()
    share_manager.get_share_url.return_value = "https://example.test/notebook/nb_123"
    api = NotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    share_manager.get_share_url.assert_called_once_with("nb_123", None)
