"""Unit tests for the legacy notebook share manager."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import notebooklm._notebooks as notebooks_module
from notebooklm._notebooks import build_share_url
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.sharing import ShareManager
from notebooklm.rpc import RPCMethod
from tests._fixtures.fake_core import make_fake_core

BASE_URL = "https://notebook.google.com"


class _TruthinessProbe:
    def __init__(self, name: str, events: list[str], *, raises: bool = False) -> None:
        self.name = name
        self.events = events
        self.raises = raises
        self.truthiness_checks = 0

    def __bool__(self) -> bool:
        self.truthiness_checks += 1
        self.events.append(self.name)
        if self.raises:
            raise RuntimeError(f"{self.name} truthiness failed")
        # A second evaluation would be false and expose stateful truthiness drift.
        return self.truthiness_checks == 1

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        return f"https://example.test/notebook/{notebook_id}?artifactId={artifact_id}"


def _make_rpc() -> AsyncMock:
    return AsyncMock(return_value=None)


def _make_manager() -> tuple[ShareManager, AsyncMock]:
    rpc = _make_rpc()
    core = make_fake_core(rpc_call=rpc)
    return (
        ShareManager(
            core.rpc_executor,
            share_url_builder=lambda notebook_id, artifact_id=None: build_share_url(
                BASE_URL, notebook_id, artifact_id
            ),
        ),
        rpc,
    )


def test_build_share_url_without_artifact() -> None:
    assert build_share_url(BASE_URL, "nb_123") == "https://notebook.google.com/notebook/nb_123"


def test_build_share_url_with_artifact() -> None:
    assert (
        build_share_url(BASE_URL, "nb_123", "art_456")
        == "https://notebook.google.com/notebook/nb_123?artifactId=art_456"
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
        "url": "https://notebook.google.com/notebook/nb_123?artifactId=art_456",
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
        "url": "https://notebook.google.com/notebook/nb_123",
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

    assert url == "https://notebook.google.com/notebook/nb_123?artifactId=art_456"
    rpc.assert_not_called()


@pytest.mark.asyncio
async def test_notebooks_api_default_share_manager_uses_late_bound_rpc_executor_call() -> None:
    """The auto-built ``_share_manager`` late-binds the executor's rpc_call.

    ``NotebooksAPI.share()`` was removed in v0.8.0 (#1363), but its default
    ``ShareManager`` remains available for the legacy internal ``SHARE_ARTIFACT``
    path and keeps the late-binding contract: the manager binds to the executor's
    ``rpc_call`` attribute lazily, so swapping it after construction must be
    honored. Driven directly through ``_share_manager.share`` (the manager stays;
    only the public wrapper was cut).
    """
    core = make_fake_core(rpc_call=AsyncMock(return_value=None))
    api = WebNotebooksAPI(core.rpc_executor, sources_api=MagicMock())
    replacement_rpc = AsyncMock(return_value=None)
    # ShareManager binds to the executor's rpc_call attribute lazily — swap
    # it to verify the late-binding contract. This is intentional behavior
    # under test, not the forbidden pattern (we're testing the binding).
    core.rpc_executor.rpc_call = replacement_rpc

    result = await api._share_manager.share("nb_123", public=True, artifact_id="art_456")

    assert result["url"] == "https://notebook.google.com/notebook/nb_123?artifactId=art_456"
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
    api = WebNotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)

    assert not hasattr(api, "share")


def test_notebooks_api_default_get_share_url_uses_transport_neutral_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = MagicMock()
    api = WebNotebooksAPI(core, sources_api=MagicMock())
    base_url_provider = MagicMock(return_value="https://notebooklm.google.com")
    share_url_builder = MagicMock(return_value="https://example.test/notebook/nb_123")
    monkeypatch.setattr(notebooks_module, "get_base_url", base_url_provider)
    monkeypatch.setattr(notebooks_module, "build_share_url", share_url_builder)

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    base_url_provider.assert_called_once_with()
    share_url_builder.assert_called_once_with("https://notebooklm.google.com", "nb_123", None)


def test_notebooks_api_get_share_url_delegates_to_injected_share_manager() -> None:
    core = MagicMock()
    share_manager = MagicMock()
    api = WebNotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)
    replacement = MagicMock(return_value="https://example.test/notebook/nb_123")
    share_manager.get_share_url = replacement

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    replacement.assert_called_once_with("nb_123", None)


def test_notebooks_api_falsey_share_manager_preserves_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = MagicMock()
    share_manager = MagicMock()
    share_manager.__bool__.return_value = False
    share_manager.get_share_url.return_value = "https://injected.test/notebook/nb_123"
    api = WebNotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)
    share_url_builder = MagicMock(return_value="https://example.test/notebook/nb_123")
    monkeypatch.setattr(notebooks_module, "build_share_url", share_url_builder)
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    assert api._share_manager is not share_manager
    share_manager.get_share_url.assert_not_called()
    share_url_builder.assert_called_once_with("https://notebook.google.com", "nb_123", None)


def test_notebooks_api_evaluates_share_manager_truthiness_once() -> None:
    class StatefulShareManager:
        def __init__(self) -> None:
            self.truthiness_checks = 0
            self.get_share_url = MagicMock(return_value="https://example.test/notebook/nb_123")

        def __bool__(self) -> bool:
            self.truthiness_checks += 1
            return self.truthiness_checks == 1

    core = MagicMock()
    share_manager = StatefulShareManager()
    api = WebNotebooksAPI(core, sources_api=MagicMock(), share_manager=share_manager)

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    assert share_manager.truthiness_checks == 1
    share_manager.get_share_url.assert_called_once_with("nb_123", None)


def test_web_notebooks_constructor_preserves_injected_truthiness_order() -> None:
    events: list[str] = []
    sources = _TruthinessProbe("sources", events)
    metadata = _TruthinessProbe("metadata", events)
    share = _TruthinessProbe("share", events)

    api = WebNotebooksAPI(
        MagicMock(),
        sources_api=sources,  # type: ignore[arg-type]
        metadata_service=metadata,  # type: ignore[arg-type]
        share_manager=share,  # type: ignore[arg-type]
    )

    assert events == ["sources", "metadata", "share"]
    assert sources.truthiness_checks == 1
    assert metadata.truthiness_checks == 1
    assert share.truthiness_checks == 1
    assert api._sources is sources
    assert api._metadata_service is metadata
    assert api._share_manager is share
    assert api.get_share_url("nb_123") == "https://example.test/notebook/nb_123?artifactId=None"
    assert events == ["sources", "metadata", "share"]


@pytest.mark.parametrize(
    ("raising_name", "expected_events"),
    [
        pytest.param("sources", ["sources"], id="sources-first"),
        pytest.param("metadata", ["sources", "metadata"], id="metadata-second"),
        pytest.param("share", ["sources", "metadata", "share"], id="share-third"),
    ],
)
def test_web_notebooks_constructor_truthiness_failures_preserve_order(
    raising_name: str,
    expected_events: list[str],
) -> None:
    events: list[str] = []
    sources = _TruthinessProbe("sources", events, raises=raising_name == "sources")
    metadata = _TruthinessProbe("metadata", events, raises=raising_name == "metadata")
    share = _TruthinessProbe("share", events, raises=raising_name == "share")

    with pytest.raises(RuntimeError, match=rf"^{raising_name} truthiness failed$"):
        WebNotebooksAPI(
            MagicMock(),
            sources_api=sources,  # type: ignore[arg-type]
            metadata_service=metadata,  # type: ignore[arg-type]
            share_manager=share,  # type: ignore[arg-type]
        )

    assert events == expected_events
    probes = {"sources": sources, "metadata": metadata, "share": share}
    for name, probe in probes.items():
        assert probe.truthiness_checks == (1 if name in expected_events else 0)


def test_notebooks_api_injected_share_url_observes_whole_manager_replacement() -> None:
    core = MagicMock()
    original_manager = MagicMock()
    api = WebNotebooksAPI(core, sources_api=MagicMock(), share_manager=original_manager)
    replacement_manager = MagicMock()
    replacement_manager.get_share_url.return_value = "https://example.test/notebook/nb_123"
    api._share_manager = replacement_manager

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    original_manager.get_share_url.assert_not_called()
    replacement_manager.get_share_url.assert_called_once_with("nb_123", None)
