"""P10 R5.1a: what the Studio generation hoist must preserve.

The eight ``artifact.generate_*`` operations used to resolve their inputs below
the port: ``source_ids is None`` triggered a ``GET_NOTEBOOK`` read, ``language
is None`` fell back to :func:`notebooklm._env.get_default_language`, and the
option vocabularies were validated inside ``_web/codec/generation.py``.  Under
ADR-0035 addendum D1(a) all three are service concerns, owned by
``_studio/generation.py``.

These tests observe the behaviour through the ``_studio`` family services — the
one surface that exists on both sides of the move — and pin:

* **per-family read/validate ordering**, which is *not* uniform: audio, quiz,
  flashcards, infographic and slide deck reject an unreviewed option *before*
  the default-source read, while report and video resolve sources *first* and
  reject afterwards;
* the **warning surface** of that read — audio is silent, every other family
  logs the schema-drift warning on the ``notebooklm._notebooks`` logger;
* the **service-level defaults** — ``None`` sources mean the notebook's whole
  embedded source set, ``None`` language means the environment default;
* the **encoded ``CREATE_ARTIFACT`` params**, pinned as SHA-256 digests so wire
  drift fails here as well as in the ``freq``-matched cassettes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._semantic.backend import BackendAdapter, BackendContractError
from notebooklm._semantic.records import (
    AudioGenerateRequest,
    DataTableGenerateRequest,
    InfographicGenerateRequest,
    InteractiveGenerateRequest,
    ReportGenerateRequest,
    SlideDeckGenerateRequest,
    VideoGenerateRequest,
)
from notebooklm._semantic.services.read import NotebookReadService
from notebooklm._studio import (
    AudioFamilyService,
    DataTableFamilyService,
    InteractiveFamilyService,
    ReportFamilyService,
    StudioCatalog,
    StudioGenerationInputs,
    VideoFamilyService,
    VisualFamilyService,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NOTEBOOK_WITH_SOURCES: list[Any] = [
    ["Notebook", [[["src-a"], "A"], [["src-b"], "B"]], "nb"],
]
_NO_SOURCES_SLOT: list[Any] = [["Notebook without a sources slot"]]
_KICKOFF: list[Any] = [["task-id", "Title", 1, None, 1]]


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]


class _RecordingExecutor:
    """Record every dispatched native and replay canned responses in order."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def methods(self) -> list[RPCMethod]:
        return [call.method for call in self.calls]

    def kickoff_params(self) -> list[Any]:
        (kickoff,) = [
            call.params for call in self.calls if call.method is RPCMethod.CREATE_ARTIFACT
        ]
        return kickoff


#: ``family -> (service call, unresolved-input factory)``.  The factory takes the
#: family's option overrides so one matrix drives every assertion below.
_Generate = Callable[[BackendAdapter, Any], Awaitable[object]]


def _inputs(backend: BackendAdapter) -> StudioGenerationInputs:
    return StudioGenerationInputs(NotebookReadService(backend))


def _audio(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = AudioFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate(value, deadline=None)


def _quiz(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = InteractiveFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate_quiz(value, deadline=None)


def _flashcards(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = InteractiveFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate_flashcards(value, deadline=None)


def _infographic(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = VisualFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate_infographic(value, deadline=None)


def _slide_deck(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = VisualFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate_slide_deck(value, deadline=None)


def _data_table(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = DataTableFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate(value, deadline=None)


def _report(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = ReportFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate(value, deadline=None)


def _video(backend: BackendAdapter, value: Any) -> Awaitable[object]:
    service = VideoFamilyService(backend, StudioCatalog(backend), _inputs(backend))
    return service.generate(value, deadline=None)


GENERATE: dict[str, _Generate] = {
    "audio": _audio,
    "quiz": _quiz,
    "flashcards": _flashcards,
    "infographic": _infographic,
    "slide_deck": _slide_deck,
    "data_table": _data_table,
    "report": _report,
    "video": _video,
}


def _input(family: str, source_ids: tuple[str, ...] | None, options: dict[str, Any]) -> Any:
    """Build one family's service input; ``language`` defaults to unresolved."""
    language = options.pop("language", None)
    if family == "audio":
        return AudioGenerateRequest("nb", source_ids, language, **options)
    if family in {"quiz", "flashcards"}:
        return InteractiveGenerateRequest("nb", source_ids, **options)
    if family == "infographic":
        return InfographicGenerateRequest("nb", source_ids, language, **options)
    if family == "slide_deck":
        return SlideDeckGenerateRequest("nb", source_ids, language, **options)
    if family == "data_table":
        return DataTableGenerateRequest("nb", source_ids, language, **options)
    if family == "report":
        return ReportGenerateRequest("nb", source_ids=source_ids, language=language, **options)
    if family == "video":
        return VideoGenerateRequest("nb", source_ids, language, **options)
    raise AssertionError(family)


def unresolved(family: str, **options: Any) -> Any:
    """Build one family's input with neither sources nor language resolved."""
    return _input(family, None, options)


def resolved(family: str, **options: Any) -> Any:
    """Build the same input with the source set already given."""
    return _input(family, ("src-a", "src-b"), options)


# --- 1. read/validate ordering is family-specific and load-bearing ----------------

#: The families that reject an unreviewed option before issuing any native call.
VALIDATE_FIRST = [
    ("audio", {"audio_format": "future"}, "unrecognized audio format"),
    ("audio", {"audio_length": "epic"}, "unrecognized audio length"),
    ("quiz", {"quantity": "dozens"}, "unrecognized interactive quantity"),
    ("flashcards", {"difficulty": "impossible"}, "unrecognized interactive difficulty"),
    ("infographic", {"orientation": "diagonal"}, "unrecognized visual orientation"),
    ("infographic", {"detail_level": "exhaustive"}, "unrecognized visual detail level"),
    ("infographic", {"style": "baroque"}, "unrecognized visual style"),
    ("slide_deck", {"slide_format": "scroll"}, "unrecognized visual format"),
    ("slide_deck", {"slide_length": "epic"}, "unrecognized visual length"),
]

#: The document families read the default source set *before* they validate.
RESOLVE_FIRST = [
    ("report", {"report_format": "novel"}, "unrecognized report format"),
    ("video", {"video_format": "imax"}, "unrecognized video option"),
    ("video", {"video_style": "cubist"}, "unrecognized video option"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "options", "match"),
    VALIDATE_FIRST,
    ids=[f"{family}-{next(iter(options))}" for family, options, _ in VALIDATE_FIRST],
)
async def test_media_families_validate_before_resolving_sources(
    family: str, options: dict[str, Any], match: str
) -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError, match=match):
        await GENERATE[family](backend, unresolved(family, **options))

    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "options", "match"),
    RESOLVE_FIRST,
    ids=[f"{family}-{next(iter(options))}" for family, options, _ in RESOLVE_FIRST],
)
async def test_document_families_resolve_sources_before_validating(
    family: str, options: dict[str, Any], match: str
) -> None:
    executor = _RecordingExecutor(_NOTEBOOK_WITH_SOURCES)
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError, match=match):
        await GENERATE[family](backend, unresolved(family, **options))

    assert executor.methods == [RPCMethod.GET_NOTEBOOK]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "options", "match"),
    RESOLVE_FIRST,
    ids=[f"{family}-{next(iter(options))}" for family, options, _ in RESOLVE_FIRST],
)
async def test_document_families_with_explicit_sources_issue_no_read(
    family: str, options: dict[str, Any], match: str
) -> None:
    """Explicit sources skip the read; the same rejection still lands."""
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError, match=match):
        await GENERATE[family](backend, resolved(family, **options))

    assert executor.calls == []


# --- 2. the default-source read and its warning surface ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(GENERATE))
async def test_none_sources_resolve_to_the_whole_notebook_source_set(family: str) -> None:
    executor = _RecordingExecutor(_NOTEBOOK_WITH_SOURCES, _KICKOFF)
    backend = build_web_backend(executor)

    await GENERATE[family](backend, unresolved(family))

    assert executor.methods == [RPCMethod.GET_NOTEBOOK, RPCMethod.CREATE_ARTIFACT]
    flattened = json.dumps(executor.kickoff_params())
    assert '"src-a"' in flattened and '"src-b"' in flattened


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(GENERATE))
async def test_explicit_sources_skip_the_default_read(family: str) -> None:
    executor = _RecordingExecutor(_KICKOFF)
    backend = build_web_backend(executor)

    await GENERATE[family](backend, resolved(family))

    assert executor.methods == [RPCMethod.CREATE_ARTIFACT]


@pytest.mark.asyncio
async def test_the_audio_default_read_is_the_only_silent_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audio swallows a shape mismatch; every other family warns once."""
    backend = build_web_backend(_RecordingExecutor(_NO_SOURCES_SLOT, _KICKOFF))
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        await GENERATE["audio"](backend, unresolved("audio"))
    assert caplog.records == []

    for family in sorted(set(GENERATE) - {"audio"}):
        caplog.clear()
        backend = build_web_backend(_RecordingExecutor(_NO_SOURCES_SLOT, _KICKOFF))
        with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
            await GENERATE[family](backend, unresolved(family))
        assert [record.getMessage() for record in caplog.records] == [
            "get_source_ids: notebook_info has no sources slot for nb (schema drift?). len=1"
        ], family


# --- 3. language defaulting --------------------------------------------------------

#: Families whose kickoff payload carries a language code.
LANGUAGE_FAMILIES = ["audio", "infographic", "slide_deck", "data_table", "report", "video"]


@pytest.mark.asyncio
@pytest.mark.parametrize("family", LANGUAGE_FAMILIES)
async def test_none_language_resolves_to_the_environment_default(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HL", "fr-CA")
    executor = _RecordingExecutor(_KICKOFF)
    backend = build_web_backend(executor)

    await GENERATE[family](backend, resolved(family, language=None))

    assert '"fr-CA"' in json.dumps(executor.kickoff_params())


@pytest.mark.asyncio
@pytest.mark.parametrize("family", LANGUAGE_FAMILIES)
async def test_an_explicit_language_is_never_overridden(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HL", "fr-CA")
    executor = _RecordingExecutor(_KICKOFF)
    backend = build_web_backend(executor)

    await GENERATE[family](backend, resolved(family, language="de"))

    flattened = json.dumps(executor.kickoff_params())
    assert '"de"' in flattened and '"fr-CA"' not in flattened


# --- 4. the encoded kickoff params -------------------------------------------------

#: ``sha256`` of ``json.dumps(params)`` for one fixed input per family, measured
#: on the commit that installed this pin.  The hoist must not move any digit.
KICKOFF_DIGESTS = {
    "audio": "0b7b47c2e551c28ddd32cda4174e3c5fcc4da51f8e1c2960c39aacb8c0c148f0",
    "data_table": "27b5f8373db43b0a46edac25bb427dac95293154689d32c2235a33f2316adce2",
    "flashcards": "b04920a0542efd043308da0c7b104fc276335778591a589a5a7d38f920cb36cc",
    "infographic": "95de6a499ee2c258e13674732517cd6ca70015deff902e5e1701f69cf19fe353",
    "quiz": "e508c0fe560650ccac23b99e99a8f1c03f64e302e28f8e8faa9c578cde088ee3",
    "report": "8bf4fdb212e81e492302f633ba164c5bfb814ae2d92238b4c36a4866f2b24b86",
    "slide_deck": "9991ea61c9a562b167bc6c58c460859f9a4387a6123511c9361f41414f3d4409",
    "video": "62fca7f57bf33413641f18353fa410f9ce4d8cdecff1bc364b87871c848018b3",
}

#: One fully-specified option set per family, exercising every encoded field.
KICKOFF_OPTIONS: dict[str, dict[str, Any]] = {
    "audio": {
        "language": "en",
        "instructions": "focus on chapter 3",
        "audio_format": "debate",
        "audio_length": "long",
    },
    "quiz": {"instructions": "hard ones", "quantity": "more", "difficulty": "hard"},
    "flashcards": {"instructions": "hard ones", "quantity": "more", "difficulty": "hard"},
    "infographic": {
        "language": "en",
        "instructions": "one page",
        "orientation": "portrait",
        "detail_level": "detailed",
        "style": "bento_grid",
    },
    "slide_deck": {
        "language": "en",
        "instructions": "ten slides",
        "slide_format": "presenter_slides",
        "slide_length": "short",
    },
    "data_table": {"language": "en", "instructions": "one row per source"},
    "report": {
        "language": "en",
        "report_format": "study_guide",
        "extra_instructions": "cite sources",
    },
    "video": {
        "language": "en",
        "instructions": "keep it short",
        "video_format": "explainer",
        "video_style": "whiteboard",
    },
}


def _digest(params: list[Any]) -> str:
    return hashlib.sha256(json.dumps(params).encode("utf-8")).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(GENERATE))
async def test_kickoff_params_are_byte_identical(family: str) -> None:
    executor = _RecordingExecutor(_KICKOFF)
    backend = build_web_backend(executor)

    await GENERATE[family](backend, resolved(family, **KICKOFF_OPTIONS[family]))

    assert _digest(executor.kickoff_params()) == KICKOFF_DIGESTS[family]


@pytest.mark.asyncio
async def test_the_cinematic_video_route_keeps_its_own_kickoff() -> None:
    executor = _RecordingExecutor(_KICKOFF)
    backend = build_web_backend(executor)

    await GENERATE["video"](backend, resolved("video", language="en", cinematic_route=True))

    assert (
        _digest(executor.kickoff_params())
        == "055d9546c0411c992508c0219530271cf7ba16f3bc4a30b80a87804c026cc996"
    )


# --- the aggregate budget -----------------------------------------------------

#: ``family -> (service class, generate method)``.  These build their own service
#: so the deadline factory can be injected, which ``GENERATE`` above does not do.
_BUDGET_FAMILIES: dict[str, tuple[Any, str]] = {
    "audio": (AudioFamilyService, "generate"),
    "quiz": (InteractiveFamilyService, "generate_quiz"),
    "flashcards": (InteractiveFamilyService, "generate_flashcards"),
    "infographic": (VisualFamilyService, "generate_infographic"),
    "slide_deck": (VisualFamilyService, "generate_slide_deck"),
    "data_table": (DataTableFamilyService, "generate"),
    "report": (ReportFamilyService, "generate"),
    "video": (VideoFamilyService, "generate"),
}


class _KwargRecordingExecutor(_RecordingExecutor):
    """Also keep each call's keyword set, which carries ``_retry_deadline``."""

    def __init__(self, *responses: object) -> None:
        super().__init__(*responses)
        self.kwargs: list[dict[str, Any]] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        return await super().rpc_call(method, params, **kwargs)


def _budget_service(family: str, backend: BackendAdapter, inputs: StudioGenerationInputs) -> Any:
    service_class, _method = _BUDGET_FAMILIES[family]
    return service_class(backend, StudioCatalog(backend), inputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(_BUDGET_FAMILIES))
async def test_the_default_source_read_and_the_kickoff_share_one_client_budget(
    family: str,
) -> None:
    """The operation left the multi-native deadline ledger in R5.1a, so the family
    service captures the budget ``WebRpcBackend`` used to seed for the row: one
    absolute identity spans ``GET_NOTEBOOK`` and ``CREATE_ARTIFACT``.
    """
    executor = _KwargRecordingExecutor(_NOTEBOOK_WITH_SOURCES, _KICKOFF)
    backend = build_web_backend(executor)
    inputs = StudioGenerationInputs(
        NotebookReadService(backend),
        deadline_factory=RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 10.0),
    )
    service = _budget_service(family, backend, inputs)

    await getattr(service, _BUDGET_FAMILIES[family][1])(unresolved(family), deadline=None)

    assert executor.methods == [RPCMethod.GET_NOTEBOOK, RPCMethod.CREATE_ARTIFACT]
    read, kickoff = executor.kwargs
    assert isinstance(read["_retry_deadline"], RuntimeDeadline)
    assert read["_retry_deadline"] is kickoff["_retry_deadline"]


@pytest.mark.asyncio
async def test_an_explicit_deadline_is_never_replaced_by_the_captured_one() -> None:
    executor = _KwargRecordingExecutor(_NOTEBOOK_WITH_SOURCES, _KICKOFF)
    backend = build_web_backend(executor)
    caller = RuntimeDeadline(timeout=40.0, started_at=10.0, monotonic=lambda: 11.0)
    inputs = StudioGenerationInputs(
        NotebookReadService(backend),
        deadline_factory=RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 10.0),
    )
    service = AudioFamilyService(backend, StudioCatalog(backend), inputs)

    await service.generate(unresolved("audio"), deadline=caller)

    assert [kwargs["_retry_deadline"] for kwargs in executor.kwargs] == [caller, caller]


@pytest.mark.asyncio
async def test_without_a_factory_the_generate_families_stay_unbounded() -> None:
    """A client with no configured timeout must not acquire one here."""
    executor = _KwargRecordingExecutor(_NOTEBOOK_WITH_SOURCES, _KICKOFF)
    backend = build_web_backend(executor)
    service = AudioFamilyService(backend, StudioCatalog(backend), _inputs(backend))

    await service.generate(unresolved("audio"), deadline=None)

    assert [kwargs["_retry_deadline"] for kwargs in executor.kwargs] == [None, None]
