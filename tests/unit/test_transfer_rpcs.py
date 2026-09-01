"""Unit tests for the #2283 web RPC family.

``AddSourcesAsync`` (``X1snv``), ``AppendSource`` (``QsNTEd``),
``CopySourcesAsync`` (``R27wvc``), ``CopyArtifactsAsync`` (``mKDdke``),
``NextStepSuggestions`` (``OcvKNc``) and ``GetArtifactCustomizationChoices``
(``sqTeoe``). Pins the param-builder shapes (each cross-checked tag-for-tag
against the live Android gRPC replies recorded in
``docs/android/copy-append-suggestion-evidence.md``), the row adapters, and the
feature-layer decode / error contracts. No network: ``rpc_call`` is a narrow
``AsyncMock`` behind the ``RpcCaller`` protocol.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.contracts import RpcCaller
from notebooklm._web.mind_maps import NoteBackedMindMapService
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.notes import NoteService
from notebooklm._web.params.artifacts import (
    build_copy_artifacts_params,
    build_customization_choices_params,
)
from notebooklm._web.params.notebooks import build_next_step_suggestions_params
from notebooklm._web.params.sources import (
    build_add_sources_async_params,
    build_append_source_params,
    build_copy_sources_params,
)
from notebooklm._web.rows.customization import (
    CustomizationChoiceRow,
    ReportPresetRow,
    unwrap_customization_choices,
)
from notebooklm._web.rows.transfers import (
    AddSourcesAsyncResponseRow,
    CopiedArtifactRow,
    CopiedSourceRow,
    SourceAckRow,
    unwrap_mapping_rows,
)
from notebooklm._web.sources.transfers import SourceTransferService
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
from notebooklm.rpc import RPCMethod
from notebooklm.types import (
    ArtifactCustomizationChoices,
    CopiedArtifact,
    CopiedSource,
    CustomizationChoice,
    NextStepSuggestion,
    ReportPreset,
    SourceStatus,
)
from tests._fixtures.fake_core import make_fake_core

OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
NB, TARGET = "nb-source", "nb-target"
SRC_A, SRC_B, SRC_NEW = "src-a", "src-b", "src-new"
ART_A, ART_NEW = "art-a", "art-new"
URL = "https://example.com/"

_LOGGER = logging.getLogger("tests.transfer_rpcs")


def _stub_source(source_id: str, url: str = URL) -> list[Any]:
    """The stub row ``AddSourcesAsync`` returns: id, url, type-only metadata."""
    return [[source_id], url, [None, None, None, None, 5]]


def _source_entry(source_id: str, title: str) -> list[Any]:
    return [[source_id], title, [None, None, None, None, 8], [None, 1]]


def _artifact_row(artifact_id: str, title: str = "ML Quiz") -> list[Any]:
    return [
        artifact_id,
        title,
        4,
        [[[SRC_A], None, 8]],
        1,
        None,
        None,
        None,
        None,
        None,
        [None, [1]],
    ]


def _rpc(return_value: Any = None, side_effect: Any = None) -> MagicMock:
    return MagicMock(
        spec=RpcCaller, rpc_call=AsyncMock(return_value=return_value, side_effect=side_effect)
    )


# ---------------------------------------------------------------------------
# Param builders — the positional mirror of the live-pinned proto tags
# ---------------------------------------------------------------------------


class TestParamBuilders:
    def test_append_source_is_doubly_nested_content_at_field_four(self) -> None:
        # AppendSourceRequest{2: SourceId, 4: SourceContent{2: PlainText{1: header, 2: body}}}
        assert build_append_source_params(SRC_A, header="H", body="B") == [
            None,
            [SRC_A],
            None,
            [None, ["H", "B"]],
        ]

    def test_copy_sources_leads_with_two_unused_fields(self) -> None:
        # CopySourcesAsyncRequest{3: repeated SourceId, 4: target_project_id}
        assert build_copy_sources_params([SRC_A, SRC_B], TARGET) == [
            None,
            None,
            [[SRC_A], [SRC_B]],
            TARGET,
        ]

    def test_add_sources_async_matches_the_batch_add_request(self) -> None:
        spec = [None, None, [URL], None, None, None, None, None, None, None, 1]
        assert build_add_sources_async_params([spec], NB) == [[spec], NB, OPTS]

    def test_copy_artifacts_uses_bare_string_ids_after_the_context(self) -> None:
        # CopyArtifactsAsyncRequest{1: RequestContext, 2: repeated string, 3: target}
        assert build_copy_artifacts_params([ART_A], TARGET) == [OPTS, [ART_A], TARGET]

    def test_customization_choices_is_context_only_unless_a_notebook_is_given(self) -> None:
        assert build_customization_choices_params() == [OPTS]
        assert build_customization_choices_params(NB) == [OPTS, NB]

    def test_next_step_suggestions_scopes_with_depth_two_wrappers(self) -> None:
        # NextStepSuggestionsRequest{2: project_id, 3: repeated InputSource{1: SourceId{1: id}}}
        assert build_next_step_suggestions_params(NB) == [None, NB]
        assert build_next_step_suggestions_params(NB, []) == [None, NB]
        assert build_next_step_suggestions_params(NB, [SRC_A, SRC_B]) == [
            None,
            NB,
            [[[SRC_A]], [[SRC_B]]],
        ]


# ---------------------------------------------------------------------------
# Row adapters
# ---------------------------------------------------------------------------


class TestTransferRows:
    def test_unwrap_mapping_rows_accepts_empty_and_rejects_drift(self) -> None:
        assert unwrap_mapping_rows(None, method_id="m", source="s") == []
        assert unwrap_mapping_rows([], method_id="m", source="s") == []
        assert unwrap_mapping_rows([None], method_id="m", source="s") == []
        assert unwrap_mapping_rows([[["x", []]]], method_id="m", source="s") == [["x", []]]
        with pytest.raises(DecodingError):
            unwrap_mapping_rows("not-a-list", method_id="m", source="s")
        with pytest.raises(DecodingError):
            unwrap_mapping_rows([42], method_id="m", source="s")

    def test_copied_source_row_reads_wrapped_or_bare_original_id(self) -> None:
        row = CopiedSourceRow([[SRC_A], _source_entry(SRC_NEW, "Copy")])
        assert row.is_well_formed
        assert row.original_id == SRC_A
        assert row.source_entry == _source_entry(SRC_NEW, "Copy")
        assert CopiedSourceRow([SRC_A, _source_entry(SRC_NEW, "Copy")]).original_id == SRC_A
        assert not CopiedSourceRow([[SRC_A]]).is_well_formed
        assert not CopiedSourceRow([[""], []]).is_well_formed
        assert not CopiedSourceRow("junk").is_well_formed

    def test_copied_artifact_row(self) -> None:
        row = CopiedArtifactRow([ART_A, _artifact_row(ART_NEW)])
        assert row.is_well_formed
        assert row.original_id == ART_A
        assert row.artifact_row == _artifact_row(ART_NEW)
        assert not CopiedArtifactRow([ART_A]).is_well_formed
        assert not CopiedArtifactRow(["", _artifact_row(ART_NEW)]).is_well_formed
        assert not CopiedArtifactRow([ART_A, "row"]).is_well_formed

    def test_add_sources_async_response_row(self) -> None:
        payload = [
            [_stub_source(SRC_A)],
            None,
            [[_stub_source(SRC_A), 0], [_stub_source(SRC_B), 7]],
        ]
        view = AddSourcesAsyncResponseRow(payload)
        assert view.source_entries == [_stub_source(SRC_A)]
        assert [ack.status for ack in view.ack_rows] == [0, 7]
        assert view.ack_rows[0].source_entry == _stub_source(SRC_A)
        assert AddSourcesAsyncResponseRow([]).source_entries == []
        assert AddSourcesAsyncResponseRow([[], None, "junk"]).ack_rows == []
        assert AddSourcesAsyncResponseRow(None).ack_rows == []


class TestCustomizationRows:
    CHOICES = [
        [
            [[[1, "Deep Dive", "Two hosts"], [2, "Brief", "Short"]]],
            [[[3, "Cinematic", "Rich"]]],
            [[[1, "Detailed Deck", "Full text"], ["bad", "row"], [2]]],
            [[["Briefing Doc", "Key insights", "Create a briefing."], ["No directive", "d", ""]]],
        ]
    ]

    def test_families_and_rows(self) -> None:
        view = unwrap_customization_choices(self.CHOICES, method_id="m", source="s")
        assert [(r.code, r.title) for r in view.audio_rows] == [(1, "Deep Dive"), (2, "Brief")]
        assert [(r.code, r.title, r.description) for r in view.video_rows] == [
            (3, "Cinematic", "Rich")
        ]
        slides = view.slide_deck_rows
        assert [r.is_well_formed for r in slides] == [True, False, False]
        reports = view.report_rows
        assert [r.is_well_formed for r in reports] == [True, False]
        assert reports[0].report_type == "Briefing Doc"
        assert reports[0].directive == "Create a briefing."

    def test_envelope_is_load_bearing_but_families_are_lenient(self) -> None:
        # The server always serves the table: a missing / non-list envelope is drift.
        for payload in (None, [], [None], "junk", [42]):
            with pytest.raises(DecodingError):
                unwrap_customization_choices(payload, method_id="m", source="s")
        # Inside a recognised envelope, absent / malformed families are just empty.
        for payload in ([[None, None, None, None]], [["x", 1, 2, 3]], [[]]):
            view = unwrap_customization_choices(payload, method_id="m", source="s")
            assert view.audio_rows == []
            assert view.video_rows == []
            assert view.slide_deck_rows == []
            assert view.report_rows == []

    def test_row_defaults(self) -> None:
        assert CustomizationChoiceRow([]).code is None
        assert CustomizationChoiceRow([1]).title == ""
        assert CustomizationChoiceRow([1, 2, 3]).title == ""
        assert not CustomizationChoiceRow([True, "Bool code"]).is_well_formed
        assert ReportPresetRow(["a"]).directive == ""
        assert not ReportPresetRow(["a", "b"]).is_well_formed


# ---------------------------------------------------------------------------
# SourceTransferService (the web feature layer behind WebSourcesAPI)
# ---------------------------------------------------------------------------


def _service_kwargs(rpc: MagicMock) -> dict[str, Any]:
    return {"rpc": rpc, "logger": _LOGGER}


class TestAddUrlsAsync:
    @pytest.mark.asyncio
    async def test_queues_urls_and_returns_stub_rows(self) -> None:
        payload = [
            [_stub_source(SRC_A), _stub_source(SRC_B, "https://youtu.be/abc")],
            None,
            [[_stub_source(SRC_A), 0], [_stub_source(SRC_B, "https://youtu.be/abc"), 0]],
        ]
        rpc = _rpc(payload)
        sources = await SourceTransferService().add_urls_async(
            NB,
            [URL, "https://youtu.be/abc"],
            rpc=rpc,
            extract_youtube_video_id=lambda url: "abc" if "youtu" in url else None,
            logger=_LOGGER,
        )
        assert [s.id for s in sources] == [SRC_A, SRC_B]
        # Stub rows carry no status block; the contract is "still processing".
        assert all(s.status is SourceStatus.PROCESSING for s in sources)
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.ADD_SOURCES_ASYNC
        assert params == [
            [
                [None, None, [URL], None, None, None, None, None, None, None, 1],
                [None, None, None, None, None, None, None, ["https://youtu.be/abc"], None, None, 1],
            ],
            NB,
            OPTS,
        ]
        kwargs = rpc.rpc_call.await_args.kwargs
        assert kwargs["source_path"] == f"/notebook/{NB}"
        assert kwargs["allow_null"] is False
        assert kwargs["disable_internal_retries"] is True

    @pytest.mark.asyncio
    async def test_empty_and_blank_inputs(self) -> None:
        rpc = _rpc()
        assert (
            await SourceTransferService().add_urls_async(
                NB, [], rpc=rpc, extract_youtube_video_id=lambda _u: None, logger=_LOGGER
            )
            == []
        )
        rpc.rpc_call.assert_not_awaited()
        with pytest.raises(ValidationError):
            await SourceTransferService().add_urls_async(
                NB, [URL, " "], rpc=rpc, extract_youtube_video_id=lambda _u: None, logger=_LOGGER
            )

    @pytest.mark.asyncio
    async def test_no_rows_is_a_decode_failure_not_success(self) -> None:
        rpc = _rpc([[], None, []])
        with pytest.raises(DecodingError):
            await SourceTransferService().add_urls_async(
                NB, [URL], rpc=rpc, extract_youtube_video_id=lambda _u: None, logger=_LOGGER
            )

    @pytest.mark.asyncio
    async def test_transport_loss_is_unconfirmed_not_retried(self) -> None:
        rpc = _rpc(side_effect=NetworkError("boom"))
        with pytest.raises(RPCError, match="UNRESOLVED") as excinfo:
            await SourceTransferService().add_urls_async(
                NB, [URL], rpc=rpc, extract_youtube_video_id=lambda _u: None, logger=_LOGGER
            )
        assert excinfo.value.method_id == RPCMethod.ADD_SOURCES_ASYNC.value
        rpc.rpc_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_zero_ack_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rpc = _rpc([[_stub_source(SRC_A)], None, [[_stub_source(SRC_A), 3]]])
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            sources = await SourceTransferService().add_urls_async(
                NB, [URL], rpc=rpc, extract_youtube_video_id=lambda _u: None, logger=_LOGGER
            )
        assert [s.id for s in sources] == [SRC_A]
        assert "status 3" in caplog.text


class TestAppendText:
    @pytest.mark.asyncio
    async def test_sends_header_and_body_and_tolerates_empty_reply(self) -> None:
        rpc = _rpc(None)
        await SourceTransferService().append_text(NB, SRC_A, "more", header="H", rpc=rpc)
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.APPEND_SOURCE
        assert params == [None, [SRC_A], None, [None, ["H", "more"]]]
        kwargs = rpc.rpc_call.await_args.kwargs
        # The #2290 guard: an empty success is fine, a status-bearing null is not.
        assert kwargs["allow_null"] is True
        assert kwargs["raise_on_null_status"] is True
        assert kwargs["disable_internal_retries"] is True

    @pytest.mark.asyncio
    async def test_validation(self) -> None:
        rpc = _rpc(None)
        with pytest.raises(ValidationError):
            await SourceTransferService().append_text(NB, "", "x", header="", rpc=rpc)
        with pytest.raises(ValidationError):
            await SourceTransferService().append_text(NB, SRC_A, "", header="", rpc=rpc)
        rpc.rpc_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_loss_is_unconfirmed(self) -> None:
        rpc = _rpc(side_effect=ServerError("500"))
        with pytest.raises(RPCError, match="AppendSource may have committed"):
            await SourceTransferService().append_text(NB, SRC_A, "x", header="", rpc=rpc)


class TestCopySources:
    @pytest.mark.asyncio
    async def test_maps_original_to_copy(self) -> None:
        rpc = _rpc([[[[SRC_A], _source_entry(SRC_NEW, "Python Programming")]]])
        copied = await SourceTransferService().copy(NB, [SRC_A], TARGET, **_service_kwargs(rpc))
        assert copied == [CopiedSource(original_id=SRC_A, source=copied[0].source)]
        assert copied[0].source.id == SRC_NEW
        assert copied[0].source.title == "Python Programming"
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.COPY_SOURCES
        assert params == [None, None, [[SRC_A]], TARGET]
        assert rpc.rpc_call.await_args.kwargs["source_path"] == f"/notebook/{NB}"

    @pytest.mark.asyncio
    async def test_empty_mapping_is_not_found(self) -> None:
        for payload in (None, [], [[]]):
            with pytest.raises(SourceNotFoundError):
                await SourceTransferService().copy(
                    NB, [SRC_A], TARGET, **_service_kwargs(_rpc(payload))
                )

    @pytest.mark.asyncio
    async def test_partial_mapping_returns_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rpc = _rpc([[[[SRC_A], _source_entry(SRC_NEW, "Copy")]]])
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            copied = await SourceTransferService().copy(
                NB, [SRC_A, SRC_B], TARGET, **_service_kwargs(rpc)
            )
        assert [c.original_id for c in copied] == [SRC_A]
        assert SRC_B in caplog.text

    @pytest.mark.asyncio
    async def test_malformed_entry_fails_closed(self) -> None:
        rpc = _rpc([[[[SRC_A]]]])
        with pytest.raises(DecodingError):
            await SourceTransferService().copy(NB, [SRC_A], TARGET, **_service_kwargs(rpc))
        rpc = _rpc([[[[SRC_A], [[""], "no id"]]]])
        with pytest.raises(DecodingError):
            await SourceTransferService().copy(NB, [SRC_A], TARGET, **_service_kwargs(rpc))

    @pytest.mark.asyncio
    async def test_validation_and_transport_loss(self) -> None:
        rpc = _rpc()
        for bad in ([], [""]):
            with pytest.raises(ValidationError):
                await SourceTransferService().copy(NB, bad, TARGET, **_service_kwargs(rpc))
        with pytest.raises(ValidationError):
            await SourceTransferService().copy(NB, [SRC_A], "", **_service_kwargs(rpc))
        rpc.rpc_call.assert_not_awaited()
        with pytest.raises(RPCError, match="CopySourcesAsync may have committed"):
            await SourceTransferService().copy(
                NB, [SRC_A], TARGET, **_service_kwargs(_rpc(side_effect=NetworkError("x")))
            )


# ---------------------------------------------------------------------------
# WebArtifactsAPI.copy / get_customization_choices
# ---------------------------------------------------------------------------


def _artifacts(rpc_call: AsyncMock) -> WebArtifactsAPI:
    core = make_fake_core(rpc_call=rpc_call)
    return WebArtifactsAPI(
        rpc=core.rpc_executor,
        supervisor=core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )


class TestCopyArtifacts:
    @pytest.mark.asyncio
    async def test_maps_original_to_full_new_row(self) -> None:
        rpc_call = AsyncMock(return_value=[[[ART_A, _artifact_row(ART_NEW)]]])
        copied = await _artifacts(rpc_call).copy(NB, [ART_A], TARGET)
        assert isinstance(copied[0], CopiedArtifact)
        assert copied[0].original_id == ART_A
        assert copied[0].artifact.id == ART_NEW
        assert copied[0].artifact.title == "ML Quiz"
        method, params = rpc_call.await_args.args[:2]
        assert method is RPCMethod.COPY_ARTIFACTS
        assert params == [OPTS, [ART_A], TARGET]
        kwargs = rpc_call.await_args.kwargs
        assert kwargs["raise_on_null_status"] is True
        assert kwargs["disable_internal_retries"] is True

    @pytest.mark.asyncio
    async def test_empty_mapping_is_not_found(self) -> None:
        with pytest.raises(ArtifactNotFoundError):
            await _artifacts(AsyncMock(return_value=[])).copy(NB, [ART_A], TARGET)

    @pytest.mark.asyncio
    async def test_partial_warns_and_malformed_fails_closed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            copied = await _artifacts(
                AsyncMock(return_value=[[[ART_A, _artifact_row(ART_NEW)]]])
            ).copy(NB, [ART_A, "art-b"], TARGET)
        assert [c.original_id for c in copied] == [ART_A]
        assert "art-b" in caplog.text
        with pytest.raises(DecodingError):
            await _artifacts(AsyncMock(return_value=[[[ART_A]]])).copy(NB, [ART_A], TARGET)

    @pytest.mark.asyncio
    async def test_validation_and_transport_loss(self) -> None:
        api = _artifacts(AsyncMock(side_effect=NetworkError("x")))
        with pytest.raises(ValidationError):
            await api.copy(NB, [], TARGET)
        with pytest.raises(ValidationError):
            await api.copy(NB, [ART_A], "")
        with pytest.raises(RPCError, match="CopyArtifactsAsync may have committed"):
            await api.copy(NB, [ART_A], TARGET)


class TestCustomizationChoices:
    @pytest.mark.asyncio
    async def test_decodes_all_four_families(self) -> None:
        rpc_call = AsyncMock(return_value=TestCustomizationRows.CHOICES)
        choices = await _artifacts(rpc_call).get_customization_choices()
        assert choices == ArtifactCustomizationChoices(
            audio=(
                CustomizationChoice(1, "Deep Dive", "Two hosts"),
                CustomizationChoice(2, "Brief", "Short"),
            ),
            video=(CustomizationChoice(3, "Cinematic", "Rich"),),
            slide_deck=(CustomizationChoice(1, "Detailed Deck", "Full text"),),
            reports=(ReportPreset("Briefing Doc", "Key insights", "Create a briefing."),),
        )
        method, params = rpc_call.await_args.args[:2]
        assert method is RPCMethod.GET_CUSTOMIZATION_CHOICES
        assert params == [OPTS]
        assert rpc_call.await_args.kwargs["source_path"] == "/"
        # A null reply is drift / rejection, never "no choices" (#2284 class).
        assert rpc_call.await_args.kwargs["allow_null"] is False

    @pytest.mark.asyncio
    async def test_notebook_id_is_forwarded_when_given(self) -> None:
        rpc_call = AsyncMock(return_value=[[[[]], [[]], [[]], [[]]]])
        choices = await _artifacts(rpc_call).get_customization_choices(NB)
        assert choices == ArtifactCustomizationChoices()
        assert rpc_call.await_args.args[1] == [OPTS, NB]
        assert rpc_call.await_args.kwargs["source_path"] == f"/notebook/{NB}"

    @pytest.mark.asyncio
    async def test_missing_or_non_list_envelope_is_drift(self) -> None:
        for payload in (None, [], "junk"):
            with pytest.raises(DecodingError):
                await _artifacts(AsyncMock(return_value=payload)).get_customization_choices()


# ---------------------------------------------------------------------------
# WebNotebooksAPI.suggest_next_steps
# ---------------------------------------------------------------------------


class TestSuggestNextSteps:
    RESPONSE = [[["Why does X?", 9], ["What about Y?", 9], ["bad-row"], [7, 9], ["", 9]]]

    @pytest.mark.asyncio
    async def test_decodes_rows_and_drops_malformed(self) -> None:
        rpc = _rpc(self.RESPONSE)
        api = WebNotebooksAPI(rpc)
        api.get_source_ids = AsyncMock()  # type: ignore[method-assign]
        result = await api.suggest_next_steps(NB)
        assert result == [
            NextStepSuggestion(question="Why does X?", type_code=9),
            NextStepSuggestion(question="What about Y?", type_code=9),
        ]
        # The server scopes to all sources itself — no GET_NOTEBOOK round-trip.
        api.get_source_ids.assert_not_awaited()
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.SUGGEST_NEXT_STEPS
        assert params == [None, NB]
        assert rpc.rpc_call.await_args.kwargs["raise_on_null_status"] is True

    @pytest.mark.asyncio
    async def test_source_scoping_and_empty_payload(self) -> None:
        rpc = _rpc(None)
        assert await WebNotebooksAPI(rpc).suggest_next_steps(NB, source_ids=[SRC_A]) == []
        assert rpc.rpc_call.await_args.args[1] == [None, NB, [[[SRC_A]]]]
        with pytest.raises(ValidationError):
            await WebNotebooksAPI(rpc).suggest_next_steps("")


# ---------------------------------------------------------------------------
# Unconfirmed-write marking across every ambiguous transport error (web)
# ---------------------------------------------------------------------------


class TestUnconfirmedMarking:
    @pytest.mark.parametrize("error", [NetworkError("x"), RateLimitError("x"), ServerError("x")])
    @pytest.mark.asyncio
    async def test_every_write_marks_transport_loss_unconfirmed(self, error: Exception) -> None:
        service = SourceTransferService()
        rpc = _rpc(side_effect=error)
        calls = (
            lambda: service.add_urls_async(
                NB, [URL], rpc=rpc, extract_youtube_video_id=lambda _u: None, logger=_LOGGER
            ),
            lambda: service.append_text(NB, SRC_A, "x", header="", rpc=rpc),
            lambda: service.copy(NB, [SRC_A], TARGET, **_service_kwargs(rpc)),
            lambda: _artifacts(AsyncMock(side_effect=error)).copy(NB, [ART_A], TARGET),
        )
        for call in calls:
            with pytest.raises(RPCError) as excinfo:
                await call()
            assert getattr(excinfo.value, "unconfirmed", False) is True
            assert excinfo.value.__cause__ is not None

    @pytest.mark.asyncio
    async def test_confirmed_rejections_are_not_marked(self) -> None:
        rejected = RPCError("rejected", method_id="x", rpc_code=3)
        rpc = _rpc(side_effect=rejected)
        with pytest.raises(RPCError) as excinfo:
            await SourceTransferService().copy(NB, [SRC_A], TARGET, **_service_kwargs(rpc))
        assert excinfo.value is rejected
        assert not getattr(excinfo.value, "unconfirmed", False)


class TestMalformedRowsAndGuards:
    @pytest.mark.asyncio
    async def test_malformed_copy_rows_are_skipped_not_fatal(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = [[[SRC_A], _source_entry(SRC_NEW, "Copy")], [[SRC_B]], [["x"], [[""], "no id"]]]
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            copied = await SourceTransferService().copy(
                NB, [SRC_A, SRC_B], TARGET, **_service_kwargs(_rpc([rows]))
            )
        assert [c.original_id for c in copied] == [SRC_A]
        assert caplog.text.count("malformed mapping entry") == 2
        with pytest.raises(DecodingError):
            await SourceTransferService().copy(
                NB, [SRC_A], TARGET, **_service_kwargs(_rpc([[[[SRC_A]]]]))
            )
        api = _artifacts(AsyncMock(return_value=[[[ART_A, _artifact_row(ART_NEW)], [ART_A]]]))
        assert [c.original_id for c in await api.copy(NB, [ART_A], TARGET)] == [ART_A]
        with pytest.raises(DecodingError):
            await _artifacts(AsyncMock(return_value=[[[ART_A]]])).copy(NB, [ART_A], TARGET)

    @pytest.mark.asyncio
    async def test_add_urls_async_count_mismatch_and_idless_stub(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        kwargs = {"extract_youtube_video_id": lambda _u: None, "logger": _LOGGER}
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            sources = await SourceTransferService().add_urls_async(
                NB,
                [URL, "https://b.example/"],
                rpc=_rpc([[_stub_source(SRC_A)], None, []]),
                **kwargs,
            )
        assert len(sources) == 1
        assert "queued 1 source(s) for 2 URL(s)" in caplog.text
        with pytest.raises(DecodingError):
            await SourceTransferService().add_urls_async(
                NB,
                [URL],
                rpc=_rpc([[[[""], URL, [None, None, None, None, 5]]], None, []]),
                **kwargs,
            )

    def test_row_guards_on_non_list_and_bool_slots(self) -> None:
        assert CopiedSourceRow(None).original_id is None
        assert CopiedSourceRow([42, []]).original_id is None
        assert CopiedSourceRow([[SRC_A], "row"]).source_entry is None
        assert CopiedArtifactRow("x").artifact_row is None
        assert CopiedArtifactRow([ART_A, 7]).artifact_row is None
        ack = SourceAckRow(None)
        assert ack.status is None and ack.source_entry is None and not ack.is_ok
        assert SourceAckRow([[], True]).status is None
        assert SourceAckRow([[], 0]).is_ok
        assert AddSourcesAsyncResponseRow([None, None, [None, 5]]).ack_rows[1].status is None

    @pytest.mark.asyncio
    async def test_next_step_rows_with_non_int_codes_are_dropped(self) -> None:
        rpc = _rpc([[["q", "9"], ["q2", True], ["ok", 9]]])
        result = await WebNotebooksAPI(rpc).suggest_next_steps(NB)
        assert result == [NextStepSuggestion(question="ok", type_code=9)]
        assert await WebNotebooksAPI(_rpc([None])).suggest_next_steps(NB) == []

    @pytest.mark.asyncio
    async def test_unknown_notebook_maps_to_notebook_not_found(self) -> None:
        rpc = _rpc(side_effect=RPCError("nope", method_id="OcvKNc", rpc_code=5))
        with pytest.raises(NotebookNotFoundError):
            await WebNotebooksAPI(rpc).suggest_next_steps(NB)
        rpc = _rpc(side_effect=RPCError("other", method_id="OcvKNc", rpc_code=3))
        with pytest.raises(RPCError) as excinfo:
            await WebNotebooksAPI(rpc).suggest_next_steps(NB)
        assert not isinstance(excinfo.value, NotebookNotFoundError)
