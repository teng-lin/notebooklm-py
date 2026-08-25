"""Unit tests for the semantic legacy notebook share manager."""

from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._backend import BackendError, BackendErrorReason
from notebooklm._notebooks import NotebooksAPI
from notebooklm._operations import CallPolicy, Operation
from notebooklm._sharing_manager import ShareManager, build_share_url
from notebooklm._sharing_records import (
    LEGACY_SHARE_ARTIFACT_DEF,
    LegacyShareArtifactInput,
    LegacyShareArtifactResult,
)
from notebooklm._web.codec.sharing import build_legacy_share_artifact_params
from notebooklm.exceptions import ServerError
from notebooklm.rpc.decoder import decode_response
from tests._fixtures.fake_core import make_fake_core
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend

BASE_URL = "https://notebook.google.com"


def _make_manager(
    *,
    public: bool = True,
    artifact_id: str | None = None,
) -> tuple[ShareManager, RecordingBackend]:
    backend = RecordingBackend()
    backend.set_result(
        LEGACY_SHARE_ARTIFACT_DEF,
        LegacyShareArtifactResult(public=public, artifact_id=artifact_id),
    )
    return ShareManager(backend, base_url_provider=lambda: BASE_URL), backend


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
    manager, backend = _make_manager(artifact_id="art_456")

    result = await manager.share("nb_123", public=True, artifact_id="art_456")

    assert result == {
        "public": True,
        "url": "https://notebook.google.com/notebook/nb_123?artifactId=art_456",
        "artifact_id": "art_456",
    }
    assert backend.invocations == [
        BackendInvocation(
            Operation.LEGACY_SHARE_ARTIFACT,
            LegacyShareArtifactInput("nb_123", True, "art_456"),
            None,
        )
    ]


@pytest.mark.asyncio
async def test_share_public_without_artifact_returns_notebook_url() -> None:
    manager, backend = _make_manager()

    result = await manager.share("nb_123")

    assert result == {
        "public": True,
        "url": "https://notebook.google.com/notebook/nb_123",
        "artifact_id": None,
    }
    assert backend.invocations == [
        BackendInvocation(
            Operation.LEGACY_SHARE_ARTIFACT,
            LegacyShareArtifactInput("nb_123", True, None),
            None,
        )
    ]


@pytest.mark.asyncio
async def test_share_private_sends_disable_payload_and_returns_no_url() -> None:
    manager, backend = _make_manager(public=False)

    result = await manager.share("nb_123", public=False)

    assert result == {"public": False, "url": None, "artifact_id": None}
    assert backend.invocations == [
        BackendInvocation(
            Operation.LEGACY_SHARE_ARTIFACT,
            LegacyShareArtifactInput("nb_123", False, None),
            None,
        )
    ]


@pytest.mark.asyncio
async def test_share_private_with_artifact_preserves_artifact_id_but_returns_no_url() -> None:
    manager, backend = _make_manager(public=False, artifact_id="art_456")

    result = await manager.share("nb_123", public=False, artifact_id="art_456")

    assert result == {"public": False, "url": None, "artifact_id": "art_456"}
    assert backend.invocations == [
        BackendInvocation(
            Operation.LEGACY_SHARE_ARTIFACT,
            LegacyShareArtifactInput("nb_123", False, "art_456"),
            None,
        )
    ]


def test_get_share_url_is_sync_and_does_not_call_rpc() -> None:
    manager, backend = _make_manager(artifact_id="art_456")

    url = manager.get_share_url("nb_123", artifact_id="art_456")

    assert url == "https://notebook.google.com/notebook/nb_123?artifactId=art_456"
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_notebooks_api_direct_construction_keeps_sync_share_url_without_rpc() -> None:
    """The public URL formatter stays available without semantic composition."""
    core = make_fake_core(rpc_call=AsyncMock(return_value=None))
    api = NotebooksAPI(sources_api=MagicMock())

    assert (
        api.get_share_url("nb_123", artifact_id="art_456")
        == "https://notebook.google.com/notebook/nb_123?artifactId=art_456"
    )
    core.rpc_executor.rpc_call.assert_not_awaited()


def test_legacy_share_operation_definition_is_closed_and_mutating() -> None:
    assert LEGACY_SHARE_ARTIFACT_DEF.key is Operation.LEGACY_SHARE_ARTIFACT
    assert LEGACY_SHARE_ARTIFACT_DEF.policy is CallPolicy.MUTATION
    assert LEGACY_SHARE_ARTIFACT_DEF.input_type is LegacyShareArtifactInput
    assert LEGACY_SHARE_ARTIFACT_DEF.output_type is LegacyShareArtifactResult


@pytest.mark.parametrize(
    ("public", "artifact_id", "expected"),
    [
        (True, "art_456", [[1], "nb_123", "art_456"]),
        (False, "art_456", [[0], "nb_123", "art_456"]),
        (True, None, [[1], "nb_123"]),
        (False, None, [[0], "nb_123"]),
        (True, "", [[1], "nb_123"]),
        (False, "", [[0], "nb_123"]),
    ],
)
def test_legacy_share_codec_preserves_conditional_artifact_slot(
    public: bool,
    artifact_id: str | None,
    expected: list[object],
) -> None:
    assert build_legacy_share_artifact_params("nb_123", public, artifact_id) == expected


def test_recorded_status_three_null_remains_a_success() -> None:
    """The real SHARE_ARTIFACT cassette's INVALID_ARGUMENT/null frame is benign."""
    raw = (
        ")]}'\n\n102\n"
        '[["wrb.fr","RGP97b",null,null,null,[3],"generic"],'
        '["di",40],["af.httprm",40,"4665468529207234717",29]]\n'
    )

    assert decode_response(raw, "RGP97b", allow_null=True) is None


@pytest.mark.asyncio
async def test_share_reconstructs_backend_server_error() -> None:
    backend = RecordingBackend()
    backend.set_error(
        LEGACY_SHARE_ARTIFACT_DEF,
        BackendError(
            "bad gateway",
            operation=Operation.LEGACY_SHARE_ARTIFACT,
            diagnostics=MappingProxyType(
                {
                    "method_id": "RGP97b",
                    "rpc_code": None,
                    "found_ids": [],
                    "raw_response": None,
                    "status_code": 502,
                }
            ),
            reason=BackendErrorReason.SERVER,
        ),
    )
    manager = ShareManager(backend, base_url_provider=lambda: BASE_URL)

    with pytest.raises(ServerError, match="bad gateway") as caught:
        await manager.share("nb_123", artifact_id="art_456")

    assert caught.value.status_code == 502
    assert caught.value.method_id == "RGP97b"


@pytest.mark.asyncio
async def test_share_without_semantic_backend_fails_before_projection() -> None:
    manager = ShareManager(None, base_url_provider=lambda: BASE_URL)

    with pytest.raises(RuntimeError, match="semantic backend was not configured"):
        await manager.share("nb_123")


def test_notebooks_api_share_method_removed_in_v080() -> None:
    """NotebooksAPI.share() was removed in v0.8.0 (#1363).

    The public wrapper that delegated to the injected ``ShareManager.share`` is
    gone; callers use ``client.sharing.set_public`` (toggle) and
    ``get_share_url`` (deep-link URL). The manager-delegation contract is still
    exercised by ``ShareManager.share`` tests above and ``get_share_url`` below.
    """
    share_manager = MagicMock()
    api = NotebooksAPI(sources_api=MagicMock(), share_manager=share_manager)

    assert not hasattr(api, "share")


def test_notebooks_api_get_share_url_delegates_to_injected_share_manager() -> None:
    share_manager = MagicMock()
    share_manager.get_share_url.return_value = "https://example.test/notebook/nb_123"
    api = NotebooksAPI(sources_api=MagicMock(), share_manager=share_manager)

    url = api.get_share_url("nb_123")

    assert url == "https://example.test/notebook/nb_123"
    share_manager.get_share_url.assert_called_once_with("nb_123", None)
