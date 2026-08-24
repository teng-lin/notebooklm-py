"""Studio leaf codec rows (P9.3 Studio domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``ARTIFACT_DOWNLOAD`` is the input-keyed row: one call per input, the native
chosen from ``value.action`` (catalog read, note-backed mind-map read, or
interactive content read).  ``ARTIFACT_WAIT`` inherits the caller's deadline —
the polling loop lives above the port in ``_studio/lifecycle.py`` (gate table
§6).  The generate members, ``ARTIFACT_RENAME``, and the ``ARTIFACT_LIST`` /
``ARTIFACT_GET`` catalog merge stay handlers until their P9.2/P9.4 slices.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._backend import BackendContractError
from ..._binding import Binding, CodecBinding, NativeCallSpec, NativeChoice
from ..._operations import Operation
from ..._records import (
    ARTIFACT_DELETE_DEF,
    ARTIFACT_DOWNLOAD_DEF,
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_WAIT_DEF,
    ArtifactDownloadInput,
)
from ...rpc import RPCMethod
from ..codec import artifacts as artifacts_codec
from ..codec import studio_documents as studio_documents_codec

_DOWNLOAD_CATALOG = NativeChoice(RPCMethod.LIST_ARTIFACTS)
_DOWNLOAD_MIND_MAPS = NativeChoice(RPCMethod.GET_NOTES_AND_MIND_MAPS)
_DOWNLOAD_CONTENT = NativeChoice(RPCMethod.GET_INTERACTIVE_HTML)


def _select_download(value: ArtifactDownloadInput) -> NativeChoice[RPCMethod]:
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

STUDIO_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        ARTIFACT_EXPORT.definition.key: ARTIFACT_EXPORT,
        ARTIFACT_REVISE_SLIDE.definition.key: ARTIFACT_REVISE_SLIDE,
        ARTIFACT_RETRY.definition.key: ARTIFACT_RETRY,
        ARTIFACT_DELETE.definition.key: ARTIFACT_DELETE,
        ARTIFACT_WAIT.definition.key: ARTIFACT_WAIT,
        ARTIFACT_DOWNLOAD.definition.key: ARTIFACT_DOWNLOAD,
    }
)

__all__ = [
    "ARTIFACT_DELETE",
    "ARTIFACT_DOWNLOAD",
    "ARTIFACT_EXPORT",
    "ARTIFACT_RETRY",
    "ARTIFACT_REVISE_SLIDE",
    "ARTIFACT_WAIT",
    "STUDIO_ROWS",
]
