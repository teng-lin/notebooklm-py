"""Studio leaf codec rows (P9.3 Studio domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``ARTIFACT_DOWNLOAD`` is the input-keyed row: one call per input, the native
chosen from ``value.action`` (catalog read, note-backed mind-map read, or
interactive content read).  ``ARTIFACT_WAIT`` inherits the caller's deadline —
the polling loop lives above the port in ``_studio/lifecycle.py`` (gate table
§6). ``ARTIFACT_PATCH_TITLE`` and ``ARTIFACT_CATALOG`` are the one-call leaves
sequenced by the service-owned ``ARTIFACT_RENAME`` workflow. Since P9.4b the
eight ``CREATE_ARTIFACT`` generate families are *deferred-product*
:class:`CustomBinding` rows; ``ARTIFACT_GENERATE_MIND_MAP`` and the
``ARTIFACT_LIST`` / ``ARTIFACT_GET`` catalog merge are custom rows in the
mind-map binding module.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._backend import BackendContractError
from ..._binding import (
    Binding,
    CodecBinding,
    CustomBinding,
    NativeCallSpec,
    RowInvoker,
    RpcNative,
)
from ..._deadline import RuntimeDeadline
from ..._env import get_default_language
from ..._operations import Operation
from ..._records import (
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_DELETE_DEF,
    ARTIFACT_DOWNLOAD_DEF,
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_GENERATE_AUDIO_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ARTIFACT_GENERATE_REPORT_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ARTIFACT_GENERATE_VIDEO_DEF,
    ARTIFACT_PATCH_TITLE_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_WAIT_DEF,
    ArtifactDownloadInput,
    AudioGenerateInput,
    AudioGenerateResult,
    DataTableGenerateInput,
    DataTableGenerateResult,
    InfographicGenerateInput,
    InteractiveGenerateInput,
    InteractiveGenerateResult,
    ReportGenerateInput,
    ReportGenerateResult,
    SlideDeckGenerateInput,
    VideoGenerateInput,
    VideoGenerateResult,
    VisualGenerateResult,
)
from ...rpc import RPCMethod
from ..codec import artifacts as artifacts_codec
from ..codec import generation as generation_codec
from ..codec import studio_documents as studio_documents_codec
from ..codec.source_ids import (
    SourceIdDiagnostics,
    decode_notebook_source_ids,
    encode_notebook_source_read,
)

_DOWNLOAD_CATALOG = RpcNative(RPCMethod.LIST_ARTIFACTS)
_DOWNLOAD_MIND_MAPS = RpcNative(RPCMethod.GET_NOTES_AND_MIND_MAPS)
_DOWNLOAD_CONTENT = RpcNative(RPCMethod.GET_INTERACTIVE_HTML)


def _select_download(value: ArtifactDownloadInput) -> RpcNative[RPCMethod]:
    """Pick the one native ``artifact.download`` issues for ``value.action``."""
    if value.action == "catalog":
        return _DOWNLOAD_CATALOG
    if value.action == "mind_maps":
        return _DOWNLOAD_MIND_MAPS
    if value.action in {"interactive_html", "mind_map_tree"}:
        return _DOWNLOAD_CONTENT
    raise BackendContractError(
        f"unrecognized artifact.download action {value.action!r}",
        operation=Operation.ARTIFACT_DOWNLOAD,
    )


ARTIFACT_EXPORT = CodecBinding(
    definition=ARTIFACT_EXPORT_DEF,
    encode=artifacts_codec.encode_artifact_export,
    decode=artifacts_codec.decode_artifact_export,
    native=NativeCallSpec.constant(RPCMethod.EXPORT_ARTIFACT),
)

ARTIFACT_REVISE_SLIDE = CodecBinding(
    definition=ARTIFACT_REVISE_SLIDE_DEF,
    encode=studio_documents_codec.encode_artifact_revise_slide,
    decode=studio_documents_codec.decode_artifact_revise_slide,
    native=NativeCallSpec.constant(RPCMethod.REVISE_SLIDE),
)

ARTIFACT_RETRY = CodecBinding(
    definition=ARTIFACT_RETRY_DEF,
    encode=studio_documents_codec.encode_artifact_retry,
    decode=studio_documents_codec.decode_artifact_retry,
    native=NativeCallSpec.constant(RPCMethod.RETRY_ARTIFACT),
)

ARTIFACT_DELETE = CodecBinding(
    definition=ARTIFACT_DELETE_DEF,
    encode=artifacts_codec.encode_artifact_delete,
    decode=artifacts_codec.decode_artifact_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_ARTIFACT),
)

ARTIFACT_PATCH_TITLE = CodecBinding(
    definition=ARTIFACT_PATCH_TITLE_DEF,
    encode=artifacts_codec.encode_artifact_patch_title,
    decode=artifacts_codec.decode_artifact_patch_title,
    native=NativeCallSpec.constant(RPCMethod.RENAME_ARTIFACT),
)

ARTIFACT_CATALOG = CodecBinding(
    definition=ARTIFACT_CATALOG_DEF,
    encode=artifacts_codec.encode_artifact_catalog_row,
    decode=artifacts_codec.decode_artifact_catalog_row,
    native=NativeCallSpec.constant(RPCMethod.LIST_ARTIFACTS),
)

ARTIFACT_WAIT = CodecBinding(
    definition=ARTIFACT_WAIT_DEF,
    encode=artifacts_codec.encode_artifact_wait,
    decode=artifacts_codec.decode_artifact_wait,
    native=NativeCallSpec.constant(RPCMethod.LIST_ARTIFACTS),
)

ARTIFACT_DOWNLOAD = CodecBinding(
    definition=ARTIFACT_DOWNLOAD_DEF,
    encode=artifacts_codec.encode_artifact_download,
    decode=artifacts_codec.decode_artifact_download,
    native=NativeCallSpec.keyed(
        _select_download,
        _DOWNLOAD_CATALOG,
        _DOWNLOAD_MIND_MAPS,
        _DOWNLOAD_CONTENT,
    ),
)

# --- P9.4b custom rows -----------------------------------------------------------
# Spec keys shared by every generate row: the conditional default-source read and
# the guarded kickoff.
_SOURCES = "sources"
_CREATE = "create"

_INPUT_DEFAULTING = (
    "Input-defaulting member kept adapter-owned under P9.2 contract 1; hoisting needs a "
    "resolved-input primitive per family (gate table §3.17)."
)


async def _default_source_ids(
    notebook_id: str,
    source_ids: tuple[str, ...] | None,
    *,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
    diagnostics: SourceIdDiagnostics,
) -> tuple[str, ...]:
    """Resolve ``source_ids is None`` through the row's ``GET_NOTEBOOK`` spec."""
    if source_ids is not None:
        return source_ids
    notebook = await invoke.call(
        _SOURCES, encode_notebook_source_read(notebook_id), deadline=deadline
    )
    return decode_notebook_source_ids(notebook, notebook_id=notebook_id, diagnostics=diagnostics)


def _language(language: str | None) -> str:
    return get_default_language() if language is None else language


async def _generate_audio(
    value: AudioGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> AudioGenerateResult:
    generation_codec.validate_audio_options(value)
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.SILENT,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_audio_generation(
            value, source_ids=source_ids, language=_language(value.language)
        ),
        deadline=deadline,
    )
    return AudioGenerateResult(
        generation_codec.decode_generation_kickoff(
            raw, operation=Operation.ARTIFACT_GENERATE_AUDIO, artifact_type="audio"
        )
    )


async def _generate_interactive(
    value: InteractiveGenerateInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
    *,
    operation: Operation,
    family: generation_codec.InteractiveFamily,
) -> InteractiveGenerateResult:
    quantity, difficulty = generation_codec.validate_interactive_options(value, operation=operation)
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.WARN,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_interactive_generation(
            value,
            family=family,
            source_ids=source_ids,
            quantity=quantity,
            difficulty=difficulty,
        ),
        deadline=deadline,
    )
    return InteractiveGenerateResult(
        generation_codec.decode_generation_kickoff(raw, operation=operation, artifact_type=family)
    )


async def _generate_quiz(
    value: InteractiveGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> InteractiveGenerateResult:
    return await _generate_interactive(
        value, deadline, invoke, operation=Operation.ARTIFACT_GENERATE_QUIZ, family="quiz"
    )


async def _generate_flashcards(
    value: InteractiveGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> InteractiveGenerateResult:
    return await _generate_interactive(
        value,
        deadline,
        invoke,
        operation=Operation.ARTIFACT_GENERATE_FLASHCARDS,
        family="flashcards",
    )


async def _generate_infographic(
    value: InfographicGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> VisualGenerateResult:
    orientation, detail_level, style = generation_codec.validate_infographic_options(value)
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.WARN,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_infographic_generation(
            value,
            source_ids=source_ids,
            language=_language(value.language),
            orientation=orientation,
            detail_level=detail_level,
            style=style,
        ),
        deadline=deadline,
    )
    return VisualGenerateResult(
        generation_codec.decode_generation_kickoff(
            raw, operation=Operation.ARTIFACT_GENERATE_INFOGRAPHIC, artifact_type="infographic"
        )
    )


async def _generate_slide_deck(
    value: SlideDeckGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> VisualGenerateResult:
    slide_format, slide_length = generation_codec.validate_slide_deck_options(value)
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.WARN,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_slide_deck_generation(
            value,
            source_ids=source_ids,
            language=_language(value.language),
            slide_format=slide_format,
            slide_length=slide_length,
        ),
        deadline=deadline,
    )
    return VisualGenerateResult(
        generation_codec.decode_generation_kickoff(
            raw, operation=Operation.ARTIFACT_GENERATE_SLIDE_DECK, artifact_type="slide deck"
        )
    )


async def _generate_data_table(
    value: DataTableGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> DataTableGenerateResult:
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.WARN,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_data_table_generation(
            value, source_ids=source_ids, language=_language(value.language)
        ),
        deadline=deadline,
    )
    return DataTableGenerateResult(
        generation_codec.decode_generation_kickoff(
            raw, operation=Operation.ARTIFACT_GENERATE_DATA_TABLE, artifact_type="data table"
        )
    )


async def _generate_report(
    value: ReportGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> ReportGenerateResult:
    # The document families resolve sources before validating options (P5.4 order).
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.WARN,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_report_kickoff(
            value, source_ids=source_ids, language=_language(value.language)
        ),
        deadline=deadline,
    )
    return ReportGenerateResult(
        generation_codec.decode_generation_kickoff(
            raw, operation=Operation.ARTIFACT_GENERATE_REPORT, artifact_type="report"
        )
    )


async def _generate_video(
    value: VideoGenerateInput, deadline: RuntimeDeadline | None, invoke: RowInvoker
) -> VideoGenerateResult:
    source_ids = await _default_source_ids(
        value.notebook_id,
        value.source_ids,
        deadline=deadline,
        invoke=invoke,
        diagnostics=SourceIdDiagnostics.WARN,
    )
    raw = await invoke.call(
        _CREATE,
        generation_codec.encode_video_kickoff(
            value, source_ids=source_ids, language=_language(value.language)
        ),
        deadline=deadline,
    )
    return VideoGenerateResult(
        generation_codec.decode_generation_kickoff(
            raw,
            operation=Operation.ARTIFACT_GENERATE_VIDEO,
            artifact_type="cinematic video" if value.cinematic_route else "video",
        )
    )


ARTIFACT_GENERATE_AUDIO = CustomBinding(
    definition=ARTIFACT_GENERATE_AUDIO_DEF,
    handler=_generate_audio,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_QUIZ = CustomBinding(
    definition=ARTIFACT_GENERATE_QUIZ_DEF,
    handler=_generate_quiz,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_FLASHCARDS = CustomBinding(
    definition=ARTIFACT_GENERATE_FLASHCARDS_DEF,
    handler=_generate_flashcards,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_REPORT = CustomBinding(
    definition=ARTIFACT_GENERATE_REPORT_DEF,
    handler=_generate_report,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_VIDEO = CustomBinding(
    definition=ARTIFACT_GENERATE_VIDEO_DEF,
    handler=_generate_video,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_INFOGRAPHIC = CustomBinding(
    definition=ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    handler=_generate_infographic,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_SLIDE_DECK = CustomBinding(
    definition=ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    handler=_generate_slide_deck,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

ARTIFACT_GENERATE_DATA_TABLE = CustomBinding(
    definition=ARTIFACT_GENERATE_DATA_TABLE_DEF,
    handler=_generate_data_table,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SOURCES),
        NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT, key=_CREATE),
    ),
    justification=_INPUT_DEFAULTING,
    category="deferred-product",
)

STUDIO_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        ARTIFACT_CATALOG.definition.key: ARTIFACT_CATALOG,
        ARTIFACT_EXPORT.definition.key: ARTIFACT_EXPORT,
        ARTIFACT_REVISE_SLIDE.definition.key: ARTIFACT_REVISE_SLIDE,
        ARTIFACT_RETRY.definition.key: ARTIFACT_RETRY,
        ARTIFACT_DELETE.definition.key: ARTIFACT_DELETE,
        ARTIFACT_PATCH_TITLE.definition.key: ARTIFACT_PATCH_TITLE,
        ARTIFACT_WAIT.definition.key: ARTIFACT_WAIT,
        ARTIFACT_DOWNLOAD.definition.key: ARTIFACT_DOWNLOAD,
        ARTIFACT_GENERATE_AUDIO.definition.key: ARTIFACT_GENERATE_AUDIO,
        ARTIFACT_GENERATE_QUIZ.definition.key: ARTIFACT_GENERATE_QUIZ,
        ARTIFACT_GENERATE_FLASHCARDS.definition.key: ARTIFACT_GENERATE_FLASHCARDS,
        ARTIFACT_GENERATE_REPORT.definition.key: ARTIFACT_GENERATE_REPORT,
        ARTIFACT_GENERATE_VIDEO.definition.key: ARTIFACT_GENERATE_VIDEO,
        ARTIFACT_GENERATE_INFOGRAPHIC.definition.key: ARTIFACT_GENERATE_INFOGRAPHIC,
        ARTIFACT_GENERATE_SLIDE_DECK.definition.key: ARTIFACT_GENERATE_SLIDE_DECK,
        ARTIFACT_GENERATE_DATA_TABLE.definition.key: ARTIFACT_GENERATE_DATA_TABLE,
    }
)

__all__ = [
    "ARTIFACT_CATALOG",
    "ARTIFACT_DELETE",
    "ARTIFACT_DOWNLOAD",
    "ARTIFACT_EXPORT",
    "ARTIFACT_GENERATE_AUDIO",
    "ARTIFACT_GENERATE_DATA_TABLE",
    "ARTIFACT_GENERATE_FLASHCARDS",
    "ARTIFACT_GENERATE_INFOGRAPHIC",
    "ARTIFACT_GENERATE_QUIZ",
    "ARTIFACT_GENERATE_REPORT",
    "ARTIFACT_GENERATE_SLIDE_DECK",
    "ARTIFACT_GENERATE_VIDEO",
    "ARTIFACT_PATCH_TITLE",
    "ARTIFACT_RETRY",
    "ARTIFACT_REVISE_SLIDE",
    "ARTIFACT_WAIT",
    "STUDIO_ROWS",
]
