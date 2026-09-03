"""Behavioural parity guardrail for ambiguous cross-backend mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._android.artifact_creation import CREATE_ARTIFACT_METHOD
from notebooklm._android.artifact_mutations import (
    EXPORT_TO_DRIVE_METHOD,
    GENERATE_ARTIFACT_METHOD,
)
from notebooklm._android.artifact_note_mind_maps import ACT_ON_SOURCES_METHOD
from notebooklm._android.artifact_transfers import COPY_ARTIFACTS_ASYNC_METHOD
from notebooklm._android.artifacts import DERIVE_ARTIFACT_METHOD, AndroidArtifactsAPI
from notebooklm._android.collections import AndroidCollectionsAPI
from notebooklm._android.labels import AndroidLabelsAPI
from notebooklm._android.notebooks import COPY_PROJECT_METHOD, AndroidNotebooksAPI
from notebooklm._android.notes import CREATE_NOTE_METHOD, AndroidNotesAPI
from notebooklm._android.organization import CREATE_LABEL_METHOD
from notebooklm._android.research import (
    DISCOVER_SOURCES_METHOD,
    START_FAST_METHOD,
    AndroidResearchAPI,
)
from notebooklm._android.sharing import SHARE_PROJECT_METHOD, AndroidSharingAPI
from notebooklm._android.source_transfers import (
    ADD_SOURCES_ASYNC_METHOD,
    APPEND_SOURCE_METHOD,
    COPY_SOURCES_ASYNC_METHOD,
)
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.collections import WebCollectionsAPI
from notebooklm._web.labels import WebLabelsAPI
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.notes import NoteService, WebNotesAPI
from notebooklm._web.research import WebResearchAPI
from notebooklm._web.sharing import WebSharingAPI
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.exceptions import NetworkError
from notebooklm.types import SharePermission
from tests._fixtures.fake_core import make_fake_core
from tests._helpers.android_supervisor import SupervisedAndroidTransport

pytestmark = pytest.mark.repo_lint

_NOTEBOOK_ID = "00000000-0000-4000-8000-000000000001"
_TARGET_NOTEBOOK_ID = "00000000-0000-4000-8000-000000000002"
_SOURCE_ID = "00000000-0000-4000-8000-000000000003"
_ARTIFACT_ID = "00000000-0000-4000-8000-000000000004"


@dataclass(frozen=True)
class _ContractCase:
    namespace: str
    method_name: str
    web_type: type[Any]
    android_type: type[Any]
    android_rpc: str
    args: tuple[Any, ...]
    kwargs: tuple[tuple[str, Any], ...] = ()
    web_results_before_error: tuple[Any, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.namespace}.{self.method_name}"


_ARTIFACT_GENERATORS = (
    "generate_audio",
    "generate_video",
    "generate_cinematic_video",
    "generate_report",
    "generate_study_guide",
    "generate_quiz",
    "generate_flashcards",
    "generate_infographic",
    "generate_slide_deck",
    "generate_data_table",
)

# Public methods that reach a non-idempotent mutation on both backends. A case
# may declare safe web preflight results before the injected mutation failure.
# Keep this explicit: a new public verb does not silently inherit the contract
# merely because it happens to call one of these methods today.
UNCONFIRMED_METHOD_MANIFEST = (
    _ContractCase(
        "notebooks",
        "copy",
        WebNotebooksAPI,
        AndroidNotebooksAPI,
        COPY_PROJECT_METHOD,
        (_NOTEBOOK_ID, "Copied notebook"),
    ),
    _ContractCase(
        "sources",
        "add_urls_async",
        WebSourcesAPI,
        AndroidSourcesAPI,
        ADD_SOURCES_ASYNC_METHOD,
        (_NOTEBOOK_ID, ["https://example.com/source"]),
    ),
    _ContractCase(
        "sources",
        "append_text",
        WebSourcesAPI,
        AndroidSourcesAPI,
        APPEND_SOURCE_METHOD,
        (_NOTEBOOK_ID, _SOURCE_ID, "append this"),
    ),
    _ContractCase(
        "sources",
        "copy",
        WebSourcesAPI,
        AndroidSourcesAPI,
        COPY_SOURCES_ASYNC_METHOD,
        (_NOTEBOOK_ID, [_SOURCE_ID], _TARGET_NOTEBOOK_ID),
    ),
    _ContractCase(
        "artifacts",
        "copy",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        COPY_ARTIFACTS_ASYNC_METHOD,
        (_NOTEBOOK_ID, [_ARTIFACT_ID], _TARGET_NOTEBOOK_ID),
    ),
    *(
        _ContractCase(
            "artifacts",
            method_name,
            WebArtifactsAPI,
            AndroidArtifactsAPI,
            CREATE_ARTIFACT_METHOD,
            (_NOTEBOOK_ID,),
            (("source_ids", [_SOURCE_ID]),),
        )
        for method_name in _ARTIFACT_GENERATORS
    ),
    _ContractCase(
        "artifacts",
        "generate_mind_map",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        ACT_ON_SOURCES_METHOD,
        (_NOTEBOOK_ID,),
        (("source_ids", [_SOURCE_ID]),),
    ),
    _ContractCase(
        "artifacts",
        "revise_slide",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        DERIVE_ARTIFACT_METHOD,
        (_NOTEBOOK_ID, _ARTIFACT_ID, 0, "clarify this slide"),
    ),
    _ContractCase(
        "artifacts",
        "retry_failed",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        GENERATE_ARTIFACT_METHOD,
        (_NOTEBOOK_ID, _ARTIFACT_ID),
    ),
    _ContractCase(
        "artifacts",
        "export_report",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        EXPORT_TO_DRIVE_METHOD,
        (_NOTEBOOK_ID, _ARTIFACT_ID),
    ),
    _ContractCase(
        "artifacts",
        "export_data_table",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        EXPORT_TO_DRIVE_METHOD,
        (_NOTEBOOK_ID, _ARTIFACT_ID),
    ),
    _ContractCase(
        "artifacts",
        "export",
        WebArtifactsAPI,
        AndroidArtifactsAPI,
        EXPORT_TO_DRIVE_METHOD,
        (_NOTEBOOK_ID, _ARTIFACT_ID),
    ),
    _ContractCase(
        "labels",
        "generate",
        WebLabelsAPI,
        AndroidLabelsAPI,
        CREATE_LABEL_METHOD,
        (_NOTEBOOK_ID,),
    ),
    _ContractCase(
        "labels",
        "create",
        WebLabelsAPI,
        AndroidLabelsAPI,
        CREATE_LABEL_METHOD,
        (_NOTEBOOK_ID, "Review topics"),
        web_results_before_error=([],),
    ),
    _ContractCase(
        "collections",
        "create",
        WebCollectionsAPI,
        AndroidCollectionsAPI,
        CREATE_LABEL_METHOD,
        ("Review collection",),
        web_results_before_error=([],),
    ),
    _ContractCase(
        "sharing",
        "set_public",
        WebSharingAPI,
        AndroidSharingAPI,
        SHARE_PROJECT_METHOD,
        (_NOTEBOOK_ID, True),
    ),
    _ContractCase(
        "sharing",
        "set_users",
        WebSharingAPI,
        AndroidSharingAPI,
        SHARE_PROJECT_METHOD,
        (_NOTEBOOK_ID, [("reader@example.com", SharePermission.VIEWER)]),
    ),
    _ContractCase(
        "sharing",
        "add_user",
        WebSharingAPI,
        AndroidSharingAPI,
        SHARE_PROJECT_METHOD,
        (_NOTEBOOK_ID, "reader@example.com"),
    ),
    _ContractCase(
        "sharing",
        "update_user",
        WebSharingAPI,
        AndroidSharingAPI,
        SHARE_PROJECT_METHOD,
        (_NOTEBOOK_ID, "reader@example.com", SharePermission.EDITOR),
    ),
    _ContractCase(
        "sharing",
        "remove_user",
        WebSharingAPI,
        AndroidSharingAPI,
        SHARE_PROJECT_METHOD,
        (_NOTEBOOK_ID, "reader@example.com"),
    ),
    _ContractCase(
        "research",
        "start",
        WebResearchAPI,
        AndroidResearchAPI,
        START_FAST_METHOD,
        (_NOTEBOOK_ID, "transport-loss contract"),
    ),
    _ContractCase(
        "research",
        "discover",
        WebResearchAPI,
        AndroidResearchAPI,
        DISCOVER_SOURCES_METHOD,
        (_NOTEBOOK_ID, "transport-loss contract"),
    ),
    _ContractCase(
        "notes",
        "create",
        WebNotesAPI,
        AndroidNotesAPI,
        CREATE_NOTE_METHOD,
        (_NOTEBOOK_ID, "Title", "Body"),
    ),
)

_WEB_SHARING_CASES = tuple(
    case for case in UNCONFIRMED_METHOD_MANIFEST if case.namespace == "sharing"
)


def _method_owner(api_type: type[Any], method_name: str) -> type[Any]:
    """Resolve a public method through the same MRO used at runtime."""

    for owner in api_type.__mro__:
        if method_name in owner.__dict__:
            return owner
    raise AssertionError(f"{api_type.__name__}.{method_name} does not resolve through its MRO")


def _build_web_api(namespace: str, side_effect: Any) -> Any:
    fake = make_fake_core(rpc_call=AsyncMock(side_effect=side_effect))
    if namespace == "notebooks":
        return WebNotebooksAPI(fake.rpc_executor, fake)
    if namespace == "sources":
        return WebSourcesAPI(
            fake.rpc_executor,
            supervisor=fake,
            uploader=MagicMock(),
        )
    if namespace == "artifacts":
        return WebArtifactsAPI(
            rpc=fake.rpc_executor,
            supervisor=fake,
            notebooks=fake,
            mind_maps=MagicMock(),
            note_service=MagicMock(),
        )
    if namespace == "labels":
        return WebLabelsAPI(fake.rpc_executor, list_sources=AsyncMock(return_value=[]))
    if namespace == "collections":
        return WebCollectionsAPI(fake.rpc_executor, list_notebooks=AsyncMock(return_value=[]))
    if namespace == "sharing":
        return WebSharingAPI(fake.rpc_executor)
    if namespace == "research":
        return WebResearchAPI(fake.rpc_executor, source_lister=fake)
    if namespace == "notes":
        service = NoteService(fake.rpc_executor, supervisor=fake)
        return WebNotesAPI(notes=service, mind_maps=MagicMock())
    raise AssertionError(f"unknown manifest namespace: {namespace}")


def _build_android_api(namespace: str, method: str, error: NetworkError) -> Any:
    transport = SupervisedAndroidTransport()
    transport.handlers[method] = error
    fake = make_fake_core()
    if namespace == "notebooks":
        return AndroidNotebooksAPI(transport, fake)
    if namespace == "sources":
        api = AndroidSourcesAPI.__new__(AndroidSourcesAPI)
        api._transport = transport
        return api
    if namespace == "artifacts":
        api = AndroidArtifactsAPI(
            session=transport,
            supervisor=transport.supervisor,
            notebooks=fake,
            mind_maps=MagicMock(),
            asset_downloads=MagicMock(),
        )
        api._require_studio_artifact_owned = AsyncMock()  # type: ignore[method-assign]
        return api
    if namespace == "labels":
        return AndroidLabelsAPI(transport, list_sources=AsyncMock(return_value=[]))
    if namespace == "collections":
        api = AndroidCollectionsAPI(transport, list_notebooks=AsyncMock(return_value=[]))
        api._list = AsyncMock(return_value=[])  # type: ignore[method-assign]
        return api
    if namespace == "sharing":
        return AndroidSharingAPI(transport)
    if namespace == "research":
        return AndroidResearchAPI(transport, fake)
    if namespace == "notes":
        return AndroidNotesAPI(transport)
    raise AssertionError(f"unknown manifest namespace: {namespace}")


async def _assert_unconfirmed(call: Callable[[], Awaitable[Any]]) -> BaseException:
    try:
        await call()
    except BaseException as exc:
        assert getattr(exc, "unconfirmed", False) is True
        return exc
    raise AssertionError("the mutation unexpectedly completed")


@pytest.mark.parametrize("case", UNCONFIRMED_METHOD_MANIFEST, ids=lambda case: case.id)
async def test_cross_backend_mutations_mark_transport_loss_unconfirmed(
    case: _ContractCase,
) -> None:
    assert _method_owner(case.web_type, case.method_name)
    assert _method_owner(case.android_type, case.method_name)

    web_error = NetworkError("response lost", method_id="web-test")
    web_side_effect: Any = (
        [*case.web_results_before_error, web_error] if case.web_results_before_error else web_error
    )
    web_api = _build_web_api(case.namespace, web_side_effect)
    web_exc = await _assert_unconfirmed(
        lambda: getattr(web_api, case.method_name)(*case.args, **dict(case.kwargs))
    )
    assert web_exc is web_error or "UNRESOLVED" in str(web_exc)

    android_error = NetworkError("response lost", method_id=case.android_rpc)
    android_api = _build_android_api(case.namespace, case.android_rpc, android_error)
    android_exc = await _assert_unconfirmed(
        lambda: getattr(android_api, case.method_name)(*case.args, **dict(case.kwargs))
    )
    assert android_exc is android_error or "UNRESOLVED" in str(android_exc)
    assert android_exc.__cause__ is None


@pytest.mark.parametrize("case", _WEB_SHARING_CASES, ids=lambda case: case.id)
async def test_web_sharing_marks_post_commit_readback_transport_loss_unconfirmed(
    case: _ContractCase,
) -> None:
    error = NetworkError("status response lost after share committed", method_id="web-test")
    api = _build_web_api("sharing", [[], error])

    exc = await _assert_unconfirmed(
        lambda: getattr(api, case.method_name)(*case.args, **dict(case.kwargs))
    )

    assert exc is error
    assert api._rpc.rpc_call.await_count == 2


async def test_web_collection_create_marks_post_commit_readback_transport_loss_unconfirmed() -> (
    None
):
    error = NetworkError("collection list lost after create committed", method_id="web-test")
    api = _build_web_api("collections", [[], None, error])

    exc = await _assert_unconfirmed(lambda: api.create("Review collection"))

    assert exc is error
    assert api._rpc.rpc_call.await_count == 3


async def test_unconfirmed_contract_negative_self_test_rejects_plain_raise() -> None:
    error = NetworkError("response lost", method_id="negative-test")

    async def unwrapped_stub() -> None:
        raise error

    with pytest.raises(AssertionError):
        await _assert_unconfirmed(unwrapped_stub)
