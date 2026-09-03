"""Unit tests for the #2283 Android gRPC family.

``AddSourcesAsync`` / ``AppendSource`` / ``CopySourcesAsync``
(:mod:`notebooklm._android.source_transfers`), ``CopyArtifactsAsync`` /
``GetArtifactCustomizationChoices`` (:mod:`notebooklm._android.artifact_transfers`)
and ``NextStepSuggestions`` (``AndroidNotebooksAPI.suggest_next_steps``). Pins
the exact request messages (tag layout live-validated in
``docs/android/copy-append-suggestion-evidence.md``), the ``replay_safe``
contract, and the decode / not-found / unconfirmed-write policies, against a
recording fake transport — no network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from google.protobuf import empty_pb2

from notebooklm._android.artifact_transfers import (
    COPY_ARTIFACTS_ASYNC_METHOD,
    GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD,
)
from notebooklm._android.artifacts import AndroidArtifactsAPI
from notebooklm._android.notebooks import NEXT_STEP_SUGGESTIONS_METHOD, AndroidNotebooksAPI
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    artifacts_pb2,
    chat_pb2,
    notebooks_pb2,
    read_pb2,
    sources_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._android.source_transfers import (
    ADD_SOURCES_ASYNC_METHOD,
    APPEND_SOURCE_METHOD,
    COPY_SOURCES_ASYNC_METHOD,
)
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm.exceptions import (
    ArtifactNotFoundError,
    DecodingError,
    NetworkError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceNotFoundError,
    ValidationError,
)
from notebooklm.types import (
    ArtifactCustomizationChoices,
    CopiedArtifact,
    CopiedSource,
    CustomizationChoice,
    NextStepSuggestion,
    ReportPreset,
    SourceStatus,
)

NB, TARGET = "nb-source", "nb-target"
SRC_A, SRC_B, SRC_NEW = "src-a", "src-b", "src-new"
ART_A, ART_NEW = "art-a", "art-new"
URL = "https://example.com/"


def is_unconfirmed(exc: BaseException) -> bool:
    """Read the ``mark_unconfirmed`` marker the way ``_app`` consumers do."""
    return bool(getattr(exc, "unconfirmed", False))


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


class FakeTransport:
    """Record every unary call and answer from a per-method queue."""

    def __init__(self, outcomes: dict[str, list[Any]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _sources_api(transport: FakeTransport) -> AndroidSourcesAPI:
    return AndroidSourcesAPI(cast(AndroidSession, transport), cast(AndroidUploadPipeline, object()))


def _notebooks_api(transport: FakeTransport) -> AndroidNotebooksAPI:
    class EmptySources:
        async def list(self, _notebook_id: str) -> list[Any]:
            return []

    return AndroidNotebooksAPI(cast(AndroidSession, transport), EmptySources())


def _artifacts_api(transport: FakeTransport) -> AndroidArtifactsAPI:
    class Notebooks:
        async def get_source_ids(self, _notebook_id: str) -> list[str]:
            return []

    class MindMaps:
        async def list_note_backed(self, *_a: Any, **_k: Any) -> list[Any]:
            return []

    supervisor = CallSupervisor(metrics=ClientMetrics(), max_concurrent_rpcs=2)
    return AndroidArtifactsAPI(
        session=cast(AndroidSession, transport),
        supervisor=supervisor,
        notebooks=cast(Any, Notebooks()),
        mind_maps=cast(Any, MindMaps()),
        asset_downloads=cast(Any, object()),
    )


def _source(source_id: str, title: str = "Example", url: str | None = None) -> read_pb2.Source:
    metadata = read_pb2.SourceMetadata(original_source_content_type=5)
    if url is not None:
        metadata.webpage_metadata.url = url
    return read_pb2.Source(
        source_id=read_pb2.SourceId(id=source_id), title=title, metadata=metadata
    )


def _artifact(artifact_id: str, title: str = "ML Quiz") -> artifacts_pb2.Artifact:
    return artifacts_pb2.Artifact(
        artifact_id=artifact_id,
        title=title,
        type=artifacts_pb2.ARTIFACT_TYPE_APP,
        status=artifacts_pb2.ARTIFACT_STATUS_READY,
    )


# ---------------------------------------------------------------------------
# AddSourcesAsync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_urls_async_sends_add_sources_request_and_decodes_stub_rows() -> None:
    reply = sources_pb2.AddSourcesAsyncResponse(
        sources=[_source(SRC_A, url=URL), _source(SRC_B, url="https://youtu.be/abc")],
        acknowledgements=[
            sources_pb2.SourceAcknowledgement(source=_source(SRC_A, url=URL), status=0),
            sources_pb2.SourceAcknowledgement(source=_source(SRC_B), status=0),
        ],
    )
    transport = FakeTransport({ADD_SOURCES_ASYNC_METHOD: [reply]})
    sources = await _sources_api(transport).add_urls_async(NB, [URL, "https://youtu.be/abc"])
    assert [s.id for s in sources] == [SRC_A, SRC_B]
    assert all(s.status is SourceStatus.PROCESSING for s in sources)
    method, request, kwargs = transport.calls[0]
    assert method == ADD_SOURCES_ASYNC_METHOD
    assert isinstance(request, sources_pb2.AddSourcesRequest)
    assert request.project_id == NB
    assert request.user_content[0].web_content.url == URL
    assert request.user_content[1].video_content.youtube_url == "https://youtu.be/abc"
    assert not request.user_content[0].HasField("tentative_source_id")
    assert request.request_context.client_type != 0
    assert kwargs == {
        "replay_safe": False,
        "response_type": sources_pb2.AddSourcesAsyncResponse,
    }
    assert transport.scopes == ["source.add_urls_async"]


@pytest.mark.asyncio
async def test_add_urls_async_policies() -> None:
    api = _sources_api(FakeTransport())
    assert await api.add_urls_async(NB, []) == []
    with pytest.raises(ValidationError):
        await api.add_urls_async(NB, [URL, ""])
    empty = FakeTransport({ADD_SOURCES_ASYNC_METHOD: [sources_pb2.AddSourcesAsyncResponse()]})
    with pytest.raises(DecodingError):
        await _sources_api(empty).add_urls_async(NB, [URL])
    lost = FakeTransport({ADD_SOURCES_ASYNC_METHOD: [NetworkError("gone")]})
    with pytest.raises(NetworkError) as excinfo:
        await _sources_api(lost).add_urls_async(NB, [URL])
    assert is_unconfirmed(excinfo.value)


# ---------------------------------------------------------------------------
# AppendSource
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_text_sends_doubly_nested_plain_text() -> None:
    transport = FakeTransport({APPEND_SOURCE_METHOD: [empty_pb2.Empty()]})
    await _sources_api(transport).append_text(NB, SRC_A, "more text", header="H")
    method, request, kwargs = transport.calls[0]
    assert method == APPEND_SOURCE_METHOD
    assert isinstance(request, sources_pb2.AppendSourceRequest)
    assert request.source_id.id == SRC_A
    assert request.content.plain_text.header == "H"
    assert request.content.plain_text.body == "more text"
    assert kwargs == {"replay_safe": False, "response_type": empty_pb2.Empty}
    # Byte-level pin of the live-proven layout: {2: SourceId{1: id}, 4: {2: {1: h, 2: b}}}.
    assert request.SerializeToString() == (
        b"\x12\x07\n\x05src-a\x22\x10\x12\x0e\n\x01H\x12\x09more text"
    )


@pytest.mark.asyncio
async def test_append_text_validation_and_unconfirmed_loss() -> None:
    api = _sources_api(FakeTransport())
    with pytest.raises(ValidationError):
        await api.append_text(NB, "", "x")
    with pytest.raises(ValidationError):
        await api.append_text(NB, SRC_A, "")
    lost = FakeTransport({APPEND_SOURCE_METHOD: [NetworkError("gone")]})
    with pytest.raises(NetworkError) as excinfo:
        await _sources_api(lost).append_text(NB, SRC_A, "x")
    assert is_unconfirmed(excinfo.value)


# ---------------------------------------------------------------------------
# CopySourcesAsync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_sources_maps_original_to_copy() -> None:
    reply = sources_pb2.CopySourcesAsyncResponse(
        copied_sources=[
            sources_pb2.CopiedSource(
                source_id=read_pb2.SourceId(id=SRC_A), source=_source(SRC_NEW, "Python")
            )
        ]
    )
    transport = FakeTransport({COPY_SOURCES_ASYNC_METHOD: [reply]})
    copied = await _sources_api(transport).copy(NB, [SRC_A], TARGET)
    assert copied == [CopiedSource(original_id=SRC_A, source=copied[0].source)]
    assert copied[0].source.id == SRC_NEW
    assert copied[0].source.title == "Python"
    method, request, kwargs = transport.calls[0]
    assert method == COPY_SOURCES_ASYNC_METHOD
    assert [s.id for s in request.source_ids] == [SRC_A]
    assert request.target_project_id == TARGET
    assert kwargs["replay_safe"] is False
    assert kwargs["response_type"] is sources_pb2.CopySourcesAsyncResponse


@pytest.mark.asyncio
async def test_copy_sources_not_found_partial_and_malformed() -> None:
    empty = FakeTransport({COPY_SOURCES_ASYNC_METHOD: [sources_pb2.CopySourcesAsyncResponse()]})
    with pytest.raises(SourceNotFoundError):
        await _sources_api(empty).copy(NB, [SRC_A], TARGET)
    partial = FakeTransport(
        {
            COPY_SOURCES_ASYNC_METHOD: [
                sources_pb2.CopySourcesAsyncResponse(
                    copied_sources=[
                        sources_pb2.CopiedSource(
                            source_id=read_pb2.SourceId(id=SRC_A), source=_source(SRC_NEW)
                        )
                    ]
                )
            ]
        }
    )
    copied = await _sources_api(partial).copy(NB, [SRC_A, SRC_B], TARGET)
    assert [c.original_id for c in copied] == [SRC_A]
    malformed = FakeTransport(
        {
            COPY_SOURCES_ASYNC_METHOD: [
                sources_pb2.CopySourcesAsyncResponse(
                    copied_sources=[sources_pb2.CopiedSource(source_id=read_pb2.SourceId(id=SRC_A))]
                )
            ]
        }
    )
    with pytest.raises(DecodingError):
        await _sources_api(malformed).copy(NB, [SRC_A], TARGET)
    api = _sources_api(FakeTransport())
    with pytest.raises(ValidationError):
        await api.copy(NB, [], TARGET)
    with pytest.raises(ValidationError):
        await api.copy(NB, [SRC_A], "")


# ---------------------------------------------------------------------------
# CopyArtifactsAsync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_artifacts_maps_original_to_new_row() -> None:
    reply = artifacts_pb2.CopyArtifactsAsyncResponse(
        copied_artifacts=[
            artifacts_pb2.CopiedArtifact(source_artifact_id=ART_A, artifact=_artifact(ART_NEW))
        ]
    )
    transport = FakeTransport({COPY_ARTIFACTS_ASYNC_METHOD: [reply]})
    copied = await _artifacts_api(transport).copy(NB, [ART_A], TARGET)
    assert isinstance(copied[0], CopiedArtifact)
    assert copied[0].original_id == ART_A
    assert copied[0].artifact.id == ART_NEW
    assert copied[0].artifact.title == "ML Quiz"
    method, request, kwargs = transport.calls[0]
    assert method == COPY_ARTIFACTS_ASYNC_METHOD
    assert isinstance(request, artifacts_pb2.CopyArtifactsAsyncRequest)
    assert list(request.artifact_ids) == [ART_A]
    assert request.target_project_id == TARGET
    assert request.request_context.client_type != 0
    assert kwargs == {
        "replay_safe": False,
        "response_type": artifacts_pb2.CopyArtifactsAsyncResponse,
        "expected_epoch": 7,
    }
    assert transport.scopes == ["artifacts.copy"]


@pytest.mark.asyncio
async def test_copy_artifacts_not_found_malformed_and_unconfirmed() -> None:
    empty = FakeTransport(
        {COPY_ARTIFACTS_ASYNC_METHOD: [artifacts_pb2.CopyArtifactsAsyncResponse()]}
    )
    with pytest.raises(ArtifactNotFoundError) as missing:
        await _artifacts_api(empty).copy(NB, [ART_A], TARGET)
    assert missing.value.method_id == COPY_ARTIFACTS_ASYNC_METHOD
    malformed = FakeTransport(
        {
            COPY_ARTIFACTS_ASYNC_METHOD: [
                artifacts_pb2.CopyArtifactsAsyncResponse(
                    copied_artifacts=[artifacts_pb2.CopiedArtifact(source_artifact_id=ART_A)]
                )
            ]
        }
    )
    with pytest.raises(DecodingError) as decoding:
        await _artifacts_api(malformed).copy(NB, [ART_A], TARGET)
    assert decoding.value.method_id == COPY_ARTIFACTS_ASYNC_METHOD
    assert decoding.value.raw_response is None
    error = NetworkError("gone")
    lost = FakeTransport({COPY_ARTIFACTS_ASYNC_METHOD: [error]})
    with pytest.raises(NetworkError) as excinfo:
        await _artifacts_api(lost).copy(NB, [ART_A], TARGET)
    assert excinfo.value is error
    assert excinfo.value.__cause__ is None
    assert is_unconfirmed(excinfo.value)
    with pytest.raises(ValidationError):
        await _artifacts_api(FakeTransport()).copy(NB, [""], TARGET)


# ---------------------------------------------------------------------------
# GetArtifactCustomizationChoices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_customization_choices_decodes_all_four_families() -> None:
    choices = artifacts_pb2.ArtifactCustomizationChoices(
        audio_overview_choices=artifacts_pb2.FormatChoices(
            choices=[
                artifacts_pb2.FormatChoice(format=1, title="Deep Dive", description="Two hosts"),
                artifacts_pb2.FormatChoice(format=2, title="", description="dropped"),
            ]
        ),
        video_overview_choices=artifacts_pb2.FormatChoices(
            choices=[artifacts_pb2.FormatChoice(format=3, title="Cinematic", description="Rich")]
        ),
        slides_customization_choices=artifacts_pb2.SlidesCustomizationChoices(
            types=[
                artifacts_pb2.SlidesType(
                    deck_type=artifacts_pb2.DECK_TYPE_PRESENTATION,
                    title="Presenter Slides",
                    description="Clean",
                )
            ]
        ),
        tailored_report_customization_choices=artifacts_pb2.TailoredReportCustomizationChoices(
            report_type_options=[
                artifacts_pb2.TailoredReportTypeOption(
                    report_type="Briefing Doc",
                    report_description="Key insights",
                    report_directive="Create a briefing.",
                ),
                artifacts_pb2.TailoredReportTypeOption(report_type="No directive"),
            ]
        ),
    )
    reply = artifacts_pb2.GetArtifactCustomizationChoicesResponse(
        artifact_customization_choices=choices
    )
    transport = FakeTransport({GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD: [reply]})
    result = await _artifacts_api(transport).get_customization_choices(NB)
    assert result == ArtifactCustomizationChoices(
        audio=(CustomizationChoice(1, "Deep Dive", "Two hosts"),),
        video=(CustomizationChoice(3, "Cinematic", "Rich"),),
        slide_deck=(CustomizationChoice(2, "Presenter Slides", "Clean"),),
        reports=(ReportPreset("Briefing Doc", "Key insights", "Create a briefing."),),
    )
    method, request, kwargs = transport.calls[0]
    assert method == GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD
    assert request.project_id == NB
    assert request.request_context.client_type != 0
    assert kwargs == {
        "replay_safe": True,
        "response_type": artifacts_pb2.GetArtifactCustomizationChoicesResponse,
    }


@pytest.mark.asyncio
async def test_customization_choices_without_notebook_sends_context_only() -> None:
    transport = FakeTransport(
        {
            GET_ARTIFACT_CUSTOMIZATION_CHOICES_METHOD: [
                artifacts_pb2.GetArtifactCustomizationChoicesResponse()
            ]
        }
    )
    result = await _artifacts_api(transport).get_customization_choices()
    assert result == ArtifactCustomizationChoices()
    _, request, _ = transport.calls[0]
    assert request.project_id == ""
    assert request.artifact_type == 0


# ---------------------------------------------------------------------------
# NextStepSuggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_next_steps_request_and_decode() -> None:
    reply = notebooks_pb2.NextStepSuggestions(
        next_steps=[
            notebooks_pb2.NextStep(suggestion="Why does X?", suggestion_type=9),
            notebooks_pb2.NextStep(suggestion="", suggestion_type=9),
            notebooks_pb2.NextStep(suggestion="What about Y?", suggestion_type=9),
        ]
    )
    transport = FakeTransport({NEXT_STEP_SUGGESTIONS_METHOD: [reply]})
    result = await _notebooks_api(transport).suggest_next_steps(NB, source_ids=[SRC_A, SRC_B])
    assert result == [
        NextStepSuggestion(question="Why does X?", type_code=9),
        NextStepSuggestion(question="What about Y?", type_code=9),
    ]
    method, request, kwargs = transport.calls[0]
    assert method == NEXT_STEP_SUGGESTIONS_METHOD
    assert isinstance(request, chat_pb2.NextStepSuggestionsRequest)
    assert request.project_id == NB
    assert [s.source_id.id for s in request.sources] == [SRC_A, SRC_B]
    assert kwargs == {"replay_safe": True, "response_type": notebooks_pb2.NextStepSuggestions}


@pytest.mark.asyncio
async def test_suggest_next_steps_without_scope_and_validation() -> None:
    transport = FakeTransport({NEXT_STEP_SUGGESTIONS_METHOD: [notebooks_pb2.NextStepSuggestions()]})
    assert await _notebooks_api(transport).suggest_next_steps(NB) == []
    _, request, _ = transport.calls[0]
    assert len(request.sources) == 0
    with pytest.raises(ValidationError):
        await _notebooks_api(FakeTransport()).suggest_next_steps("")


# ---------------------------------------------------------------------------
# Unconfirmed marking across every ambiguous transport error (Android)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [NetworkError("x"), RateLimitError("x"), ServerError("x")])
@pytest.mark.asyncio
async def test_every_android_write_marks_transport_loss_unconfirmed(error: Exception) -> None:
    for method, call in (
        (ADD_SOURCES_ASYNC_METHOD, lambda api: api.add_urls_async(NB, [URL])),
        (APPEND_SOURCE_METHOD, lambda api: api.append_text(NB, SRC_A, "x")),
        (COPY_SOURCES_ASYNC_METHOD, lambda api: api.copy(NB, [SRC_A], TARGET)),
    ):
        with pytest.raises(type(error)) as excinfo:
            await call(_sources_api(FakeTransport({method: [type(error)("gone")]})))
        assert is_unconfirmed(excinfo.value)
    with pytest.raises(type(error)) as excinfo:
        await _artifacts_api(
            FakeTransport({COPY_ARTIFACTS_ASYNC_METHOD: [type(error)("gone")]})
        ).copy(NB, [ART_A], TARGET)
    assert is_unconfirmed(excinfo.value)


@pytest.mark.asyncio
async def test_confirmed_rejections_are_not_marked_unconfirmed() -> None:
    rejected = RPCError("rejected", method_id=COPY_SOURCES_ASYNC_METHOD, rpc_code=5)
    with pytest.raises(RPCError) as excinfo:
        await _sources_api(FakeTransport({COPY_SOURCES_ASYNC_METHOD: [rejected]})).copy(
            NB, [SRC_A], TARGET
        )
    assert excinfo.value is rejected
    assert not is_unconfirmed(excinfo.value)


@pytest.mark.asyncio
async def test_partial_copies_warn_and_malformed_rows_are_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply = sources_pb2.CopySourcesAsyncResponse(
        copied_sources=[
            sources_pb2.CopiedSource(
                source_id=read_pb2.SourceId(id=SRC_A), source=_source(SRC_NEW, "Copy")
            ),
            sources_pb2.CopiedSource(source_id=read_pb2.SourceId(id=SRC_B)),  # malformed
        ]
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm._android.source_transfers"):
        copied = await _sources_api(FakeTransport({COPY_SOURCES_ASYNC_METHOD: [reply]})).copy(
            NB, [SRC_A, SRC_B], TARGET
        )
    assert [c.original_id for c in copied] == [SRC_A]
    assert "malformed mapping entry" in caplog.text
    assert "not copied: src-b" in caplog.text
    art_reply = artifacts_pb2.CopyArtifactsAsyncResponse(
        copied_artifacts=[
            artifacts_pb2.CopiedArtifact(source_artifact_id=ART_A, artifact=_artifact(ART_NEW))
        ]
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm._android.artifact_transfers"):
        copied_artifacts = await _artifacts_api(
            FakeTransport({COPY_ARTIFACTS_ASYNC_METHOD: [art_reply]})
        ).copy(NB, [ART_A, "art-b"], TARGET)
    assert [c.original_id for c in copied_artifacts] == [ART_A]
    assert "not copied: art-b" in caplog.text


@pytest.mark.asyncio
async def test_add_urls_async_warns_on_count_mismatch_and_non_zero_ack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply = sources_pb2.AddSourcesAsyncResponse(
        sources=[_source(SRC_A, url=URL)],
        acknowledgements=[sources_pb2.SourceAcknowledgement(source=_source(SRC_A), status=3)],
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm._android.source_transfers"):
        sources = await _sources_api(
            FakeTransport({ADD_SOURCES_ASYNC_METHOD: [reply]})
        ).add_urls_async(NB, [URL, "https://b.example/"])
    assert [s.id for s in sources] == [SRC_A]
    assert "queued 1 source(s) for 2 URL(s)" in caplog.text
    assert "status 3" in caplog.text
    idless = sources_pb2.AddSourcesAsyncResponse(sources=[_source("", url=URL)])
    with pytest.raises(DecodingError):
        await _sources_api(FakeTransport({ADD_SOURCES_ASYNC_METHOD: [idless]})).add_urls_async(
            NB, [URL]
        )


@pytest.mark.asyncio
async def test_suggest_next_steps_unknown_notebook_maps_to_notebook_not_found() -> None:
    missing = RPCError("nope", method_id=NEXT_STEP_SUGGESTIONS_METHOD, rpc_code=5)
    with pytest.raises(NotebookNotFoundError):
        await _notebooks_api(
            FakeTransport({NEXT_STEP_SUGGESTIONS_METHOD: [missing]})
        ).suggest_next_steps(NB)
    other = RPCError("bad", method_id=NEXT_STEP_SUGGESTIONS_METHOD, rpc_code=3)
    with pytest.raises(RPCError) as excinfo:
        await _notebooks_api(
            FakeTransport({NEXT_STEP_SUGGESTIONS_METHOD: [other]})
        ).suggest_next_steps(NB)
    assert not isinstance(excinfo.value, NotebookNotFoundError)
