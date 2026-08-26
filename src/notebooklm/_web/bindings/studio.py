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
sequenced by the service-owned ``ARTIFACT_RENAME`` workflow. Since P10 R5.1a
the eight ``CREATE_ARTIFACT`` generate families are ordinary codec rows too:
``_studio/generation.py`` resolves their source set, language and option
vocabulary above the port (ADR-0035 addendum D1(a)), so each family is one
guarded kickoff. ``ARTIFACT_GENERATE_MIND_MAP`` and the ``ARTIFACT_LIST`` /
``ARTIFACT_GET`` catalog merge are custom rows in the mind-map binding module.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._backend import BackendContractError
from ..._binding import (
    Binding,
    CodecBinding,
    NativeCallSpec,
    RpcNative,
)
from ..._operations import Operation
from ..._semantic.records import (
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
)
from ...rpc import RPCMethod
from ..codec import artifacts as artifacts_codec
from ..codec import generation as generation_codec
from ..codec import studio_documents as studio_documents_codec

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

# --- P10 R5.1a: the eight generate families as single-native codec rows ----------
# Their inputs arrive pre-resolved from ``_studio/generation.py`` (ADR-0035
# addendum D1(a)), so each family is one guarded ``CREATE_ARTIFACT`` kickoff.

ARTIFACT_GENERATE_AUDIO = CodecBinding(
    definition=ARTIFACT_GENERATE_AUDIO_DEF,
    encode=generation_codec.encode_audio_generation,
    decode=generation_codec.decode_audio_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_QUIZ = CodecBinding(
    definition=ARTIFACT_GENERATE_QUIZ_DEF,
    encode=generation_codec.encode_quiz_generation,
    decode=generation_codec.decode_quiz_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_FLASHCARDS = CodecBinding(
    definition=ARTIFACT_GENERATE_FLASHCARDS_DEF,
    encode=generation_codec.encode_flashcards_generation,
    decode=generation_codec.decode_flashcards_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_REPORT = CodecBinding(
    definition=ARTIFACT_GENERATE_REPORT_DEF,
    encode=generation_codec.encode_report_kickoff,
    decode=generation_codec.decode_report_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_VIDEO = CodecBinding(
    definition=ARTIFACT_GENERATE_VIDEO_DEF,
    encode=generation_codec.encode_video_kickoff,
    decode=generation_codec.decode_video_generation_kickoff,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_INFOGRAPHIC = CodecBinding(
    definition=ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    encode=generation_codec.encode_infographic_generation,
    decode=generation_codec.decode_infographic_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_SLIDE_DECK = CodecBinding(
    definition=ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    encode=generation_codec.encode_slide_deck_generation,
    decode=generation_codec.decode_slide_deck_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
)

ARTIFACT_GENERATE_DATA_TABLE = CodecBinding(
    definition=ARTIFACT_GENERATE_DATA_TABLE_DEF,
    encode=generation_codec.encode_data_table_generation,
    decode=generation_codec.decode_data_table_generation,
    native=NativeCallSpec.constant(RPCMethod.CREATE_ARTIFACT),
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
