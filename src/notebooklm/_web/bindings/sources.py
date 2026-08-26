"""Source read and single-native source mutation codec rows (P9.3 source domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``SOURCE_LIST``/``SOURCE_GET``/``SOURCE_WAIT`` share the recency-writing
``GET_NOTEBOOK`` snapshot; ``SOURCE_GET`` selects its exact id inside ``decode``
and ``SOURCE_WAIT`` is the one ``DeadlineMode.IGNORE`` row (source polling
historically never clamps an in-flight read).

``SOURCE_ADD_FILE`` is the family's last row, a :class:`CustomBinding`
(P9.4b): it declares exactly the natives the policy ledger lists under spec
keys (``snapshot``, ``rename``, ``register``, ``limits``) and sequences them
through the row-scoped invoker with the same options the P6.7 handlers set.
It is a *protocol* row — source registration has a tentative-source mobile
variant (ADR-0035 principle 2) and upload adds Scotty legs on top.
``source.add_text``, ``source.add_url``, ``source.add_drive`` and
``source.add_url_batch`` no longer have rows at all: P10 R3.2, R3.3, R3.4 and
R3.5 hoisted their workflows into ``SourceService`` over the
``SOURCE_REGISTER`` primitive (plus ``SOURCE_LIST`` for the URL/Drive
baseline-and-probe and the batch's ERROR-row reconciliation, and
``SOURCE_PATCH_TITLE``/``SOURCE_GET`` for the URL and Drive finalise), so the
family's one *compatibility* row is gone and its ``protocol`` count is down to
one.  That row still translates (P10 invariant I8): the
established public leaves the family owns — ``SourceAddError``, the unconfirmed
transport four-tuple, ``ValidationError``, ``NonIdempotentRetryError`` — are
captured by ``_source_add_failure`` as bounded neutral evidence under
``BackendErrorReason.SOURCE_ADD`` and replayed by ``_backend_compat`` at the
facade, so no public exception object crosses the port.  The upload pipeline is
reached only as the ``SOURCE_ADD_FILE`` row's declared collaborator and runs its
callbacks through that row's invoker for the invocation (plan open item 1); its
own Scotty legs keep their raw ``httpx`` semantics below this boundary.
``SOURCE_UPDATE`` is service-owned since P9.2-4 and hydrates through the
``SOURCE_GET`` row on a null patch-title echo.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from ..._backend import BackendContractError, BackendError, BackendErrorReason
from ..._binding import (
    Binding,
    CodecBinding,
    CodecPayload,
    CustomBinding,
    DeadlineMode,
    NativeCallSpec,
    RowInvoker,
)
from ..._deadline import RuntimeDeadline
from ..._operations import Operation
from ..._semantic.projectors import project_source
from ..._semantic.records import (
    SOURCE_ADD_FILE_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_WAIT_DEF,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceFileInputKind,
    SourceFileRegistrationRecord,
    SourceRecord,
)
from ..._source_upload_port import SourceUploadBackend
from ..._types.sources import _SOURCE_TYPE_CODE_MAP, SourceType
from ...exceptions import NetworkError, NotebookLMError, ValidationError
from ...rpc import RPCError, RPCMethod
from ...rpc.types import drive_source_status_to_str, source_status_to_str
from ...types import Source
from ..codec import settings as settings_codec
from ..codec import sources as sources_codec
from ..failure_projection import _capture_public_failure

source_logger = logging.getLogger("notebooklm").getChild("_sources")

SOURCE_LIST = CodecBinding(
    definition=SOURCE_LIST_DEF,
    encode=sources_codec.encode_source_list,
    decode=sources_codec.decode_source_list,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
)

SOURCE_GET = CodecBinding(
    definition=SOURCE_GET_DEF,
    encode=sources_codec.encode_source_get,
    decode=sources_codec.decode_source_get,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
)

SOURCE_WAIT = CodecBinding(
    definition=SOURCE_WAIT_DEF,
    encode=sources_codec.encode_source_wait,
    decode=sources_codec.decode_source_wait,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
    deadline=DeadlineMode.IGNORE,
)

SOURCE_DELETE = CodecBinding(
    definition=SOURCE_DELETE_DEF,
    encode=sources_codec.encode_source_delete,
    decode=sources_codec.decode_source_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_SOURCE),
)

SOURCE_REFRESH = CodecBinding(
    definition=SOURCE_REFRESH_DEF,
    encode=sources_codec.encode_source_refresh,
    decode=sources_codec.decode_source_refresh,
    native=NativeCallSpec.constant(RPCMethod.REFRESH_SOURCE),
)

SOURCE_CHECK_FRESHNESS = CodecBinding(
    definition=SOURCE_CHECK_FRESHNESS_DEF,
    encode=sources_codec.encode_source_check_freshness,
    decode=sources_codec.decode_source_check_freshness,
    native=NativeCallSpec.constant(RPCMethod.CHECK_SOURCE_FRESHNESS),
)

SOURCE_GET_GUIDE = CodecBinding(
    definition=SOURCE_GET_GUIDE_DEF,
    encode=sources_codec.encode_source_get_guide,
    decode=sources_codec.decode_source_get_guide,
    native=NativeCallSpec.constant(RPCMethod.GET_SOURCE_GUIDE),
)

SOURCE_GET_FULLTEXT = CodecBinding(
    definition=SOURCE_GET_FULLTEXT_DEF,
    encode=sources_codec.encode_source_get_fulltext,
    decode=sources_codec.decode_source_get_fulltext,
    native=NativeCallSpec.constant(RPCMethod.GET_SOURCE),
)


# --- source-add custom rows (P9.4b) ------------------------------------------------

_SNAPSHOT = "snapshot"
_RENAME = "rename"
_REGISTER = "register"
_LIMITS = "limits"
_SOURCE_UPLOADER = "source_uploader"


def _source_add_failure(exc: NotebookLMError, operation: Operation) -> BackendError:
    """Neutralise one source-add public failure at the row boundary (I8).

    The source-add family owns established public leaves (``SourceAddError``
    and the transport four-tuple after a partial upload, ``ValidationError`` on
    rejected input, ``NonIdempotentRetryError`` on a refused replay) that the
    shared ``translate_web_error`` families deliberately do not classify.  The
    row therefore captures the bounded public graph itself and reports one
    neutral ``SOURCE_ADD`` reason; ``_backend_compat`` replays an equal — not
    identical — public exception at the facade.  Nothing above the port sees a
    public exception object escape the backend.

    The projector is imported directly rather than reached through the
    ``capture_public_failure`` row collaborator, which left
    ``ROW_COLLABORATOR_NAMES`` with the last source-add hoist (P10 R3.5) while
    ``SOURCE_ADD_FILE`` — which needs this — is permanent under D4.  Both reach
    the same ``_web.failure_projection`` function; the collaborator seam bought
    a custom row nothing that a sibling ``_web`` import does not already give it.
    """
    return BackendError(
        # Structured subclasses render their diagnostic fields in ``__str__``;
        # store only the base message so the compatibility projector reattaches
        # them exactly once, exactly as ``translate_web_error`` does.
        message=str(exc.args[0]) if exc.args else "",
        operation=operation,
        outcome_unknown=bool(getattr(exc, "unconfirmed", False)),
        diagnostics=MappingProxyType(
            {"source_add_failure": _capture_public_failure(exc, operation=operation)}
        ),
        reason=BackendErrorReason.SOURCE_ADD,
        # ``WebTransport`` tags every native failure that escaped the runtime.
        dispatched=bool(getattr(exc, "dispatched", False)),
    )


def _source_record(source: Source) -> SourceRecord:
    """Project a public source into its transport-neutral backend record."""
    type_code = source._type_code
    kind = (
        SourceType.UNKNOWN
        if type_code is None
        else _SOURCE_TYPE_CODE_MAP.get(type_code, SourceType.UNKNOWN)
    )
    unrecognized_kind: int | str | None = (
        type_code if type_code is not None and type_code not in _SOURCE_TYPE_CODE_MAP else None
    )
    return SourceRecord(
        id=source.id,
        title=source.title,
        url=source.url,
        kind=kind.value,
        unrecognized_kind=unrecognized_kind,
        kind_present=type_code is not None,
        created_at=source.created_at,
        status=source_status_to_str(source.status),
        drive_document_id=source.drive_document_id,
        drive_status=(
            drive_source_status_to_str(source.drive_status)
            if source.drive_status is not None
            else None
        ),
        download_url=source.download_url,
        viewer_url=source.viewer_url,
        content_mime=source.content_mime,
        word_count=source.word_count,
        revision_id=source.revision_id,
        revision_timestamp=source.revision_timestamp,
        last_modified_at=source.last_modified_at,
    )


async def _snapshot_sources(
    invoke: RowInvoker,
    notebook_id: str,
    *,
    deadline: RuntimeDeadline | None,
) -> list[Source]:
    """One recency-writing snapshot under the row's ``snapshot`` spec."""
    payload = await invoke.call(
        _SNAPSHOT,
        sources_codec.encode_source_snapshot_payload(notebook_id),
        deadline=deadline,
    )
    records = sources_codec.decode_source_snapshot(
        notebook_id,
        payload,
        strict=False,
        logger=source_logger,
    )
    return [project_source(record) for record in records]


async def _rename_source(
    invoke: RowInvoker,
    notebook_id: str,
    source_id: str,
    new_title: str,
    *,
    deadline: RuntimeDeadline | None,
    hydrate_on_null: bool,
) -> Source | None:
    """The optional post-create title set-op, hydrating a null echo on request."""
    payload = await invoke.call(
        _RENAME,
        sources_codec.encode_rename_source_payload(notebook_id, source_id, new_title),
        deadline=deadline,
    )
    if payload:
        return project_source(sources_codec.decode_renamed_source(payload))
    if not hydrate_on_null:
        return None
    sources = await _snapshot_sources(invoke, notebook_id, deadline=deadline)
    source = next((source for source in sources if source.id == source_id), None)
    if source is None:
        raise sources_codec.rename_target_missing(source_id)
    return source


RenameSource = Callable[[str, str, str], Awaitable[Source | None]]


async def _honor_requested_title(
    rename: RenameSource,
    notebook_id: str,
    source: Source,
    requested_title: str | None,
    logger: logging.Logger,
) -> Source:
    """Best-effort post-add rename so an explicit ``title`` survives backend
    re-derivation (#1960).

    YouTube, native Google Drive, and web-page imports re-derive the display
    title server-side (from the video / Drive / page metadata), silently
    discarding the ``title`` sent with the add. Live-verified (URL, YouTube, and
    Drive): the backend derives the title *synchronously* — the added source comes
    back already carrying the re-derived title — so a follow-up ``rename`` lands
    after that derivation and sticks. When an explicit ``title`` differs from the
    one the add returned, issue the rename so the requested title wins.

    Only the ``SOURCE_ADD_FILE`` row reaches this: P10 R3.2-R3.5 hoisted the
    text/URL/Drive/batch workflows above the port, where
    ``SourceService._honor_requested_title`` owns the same contract over
    ``SourceRecord``s and neutral ``BackendError`` reasons. This copy stays
    below the port with the row that stays custom under decision D4, and speaks
    the wire vocabulary that placement implies — the public ``Source`` the
    upload pipeline hands back, and the raw ``RPCError``/``NetworkError``
    families its callbacks raise.

    Non-fatal by contract: the add already succeeded, so a rename failure keeps
    the added source (with its upstream title) and logs a warning rather than
    raising — callers detect the miss by comparing the returned ``source.title``
    against the title they requested (the MCP tool surfaces this).
    """
    if not requested_title:
        return source
    requested = requested_title.strip()
    if not requested or source.title == requested:
        return source
    try:
        renamed = await rename(notebook_id, source.id, requested)
    except (RPCError, NetworkError):
        logger.warning(
            "Source %s added but rename to %r failed; keeping upstream title %r",
            source.id,
            requested,
            source.title,
            exc_info=True,
        )
        return source
    # UPDATE_SOURCE's echo can be sparse (id + title only), so returning it wholesale
    # would drop url / kind / status. Keep the fully-hydrated added source and swap in
    # just the new title — mirrors the file-upload rename (``_source/upload.py``).
    return replace(source, title=(renamed.title if renamed else None) or requested)


def upload_backend(invoke: RowInvoker) -> SourceUploadBackend:
    """The upload pipeline's callbacks over one ``SOURCE_ADD_FILE`` row invoker.

    The row binds a fresh one per invocation; the web backend also installs one
    as the pipeline's default at construction so the legacy registration helper
    keeps executing under the row's declared natives.  Upload legs keep their
    own independent windows: every callback dispatches with ``deadline=None``
    exactly as the P6.7 shell methods did.
    """

    async def list_sources(notebook_id: str) -> list[Source]:
        return await _snapshot_sources(invoke, notebook_id, deadline=None)

    async def register_file_source(notebook_id: str, filename: str) -> SourceFileRegistrationRecord:
        payload = await invoke.call(
            _REGISTER,
            sources_codec.encode_register_file_source_payload(filename, notebook_id),
            deadline=None,
            disable_internal_retries=True,
        )
        return sources_codec.decode_file_registration(payload, filename=filename)

    async def rename_source(notebook_id: str, source_id: str, new_title: str) -> Source | None:
        return await _rename_source(
            invoke,
            notebook_id,
            source_id,
            new_title,
            deadline=None,
            hydrate_on_null=False,
        )

    async def get_source_limit() -> int | None:
        result = await invoke.call(
            _LIMITS,
            CodecPayload(params=settings_codec.encode_get_user_settings(), source_path="/"),
            deadline=None,
        )
        return settings_codec.decode_account_limits(result).source_limit

    return SourceUploadBackend(
        list_sources=list_sources,
        register_file_source=register_file_source,
        rename_source=rename_source,
        get_source_limit=get_source_limit,
    )


async def _add_file(
    value: SourceAddFileInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SourceAddFileResult:
    del deadline  # upload/session timeouts retain their existing independent windows
    uploader = invoke.collaborator(_SOURCE_UPLOADER)
    if uploader is None:
        raise BackendContractError(
            "source.add_file requires the composition-root upload pipeline",
            operation=Operation.SOURCE_ADD_FILE,
        )
    try:
        backend = upload_backend(invoke)
        if value.finalize_source is not None:
            source = await _honor_requested_title(
                backend.rename_source,
                value.notebook_id,
                project_source(value.finalize_source),
                value.title,
                source_logger,
            )
            return SourceAddFileResult(_source_record(source))
        if value.wait and value.title is not None and not value.title.strip():
            # The title is deferred until after the facade-owned readiness wait,
            # but validation must still happen before upload registration.
            raise ValidationError("Title cannot be empty or whitespace-only")
        transient_error_types: tuple[int | None, ...] = ()
        deferred_title: str | None = None
        # Open item 1: every callback the pipeline issues for this invocation —
        # registration, listing, rename, limit lookup — runs through this row's
        # invoker under its declared specs and failure tagging.
        with uploader.bind_backend(backend):
            if value.kind is SourceFileInputKind.LOCAL:
                if value.file_path is None:
                    raise BackendContractError(
                        "local source.add_file input lacks file_path",
                        operation=Operation.SOURCE_ADD_FILE,
                    )
                upload_result = await uploader._add_file_result(
                    value.notebook_id,
                    value.file_path,
                    mime_type=value.mime_type,
                    wait=False,
                    wait_timeout=value.wait_timeout,
                    title=(None if value.wait else value.title),
                    on_progress=value.on_progress,
                )
                source = upload_result.source
                transient_error_types = upload_result.transient_error_types
            else:
                if value.document_id is None:
                    raise BackendContractError(
                        "Drive source.add_file input lacks document_id",
                        operation=Operation.SOURCE_ADD_FILE,
                    )

                async def add_downloaded_file(
                    notebook_id: str,
                    file_path: Any,
                    *,
                    title: str | None,
                    wait: bool,
                    wait_timeout: float,
                ) -> Source:
                    nonlocal deferred_title, transient_error_types
                    upload_title = title
                    if value.wait:
                        # DriveImportService resolves a missing public title to the
                        # Drive filename. Retain that choice for the facade-owned
                        # post-readiness rename, but do not let the upload pipeline
                        # perform its own registration wait + rename first.
                        deferred_title = title.strip() if title else None
                        upload_title = None
                    upload_result = await uploader._add_file_result(
                        notebook_id,
                        file_path,
                        title=upload_title,
                        wait=wait,
                        wait_timeout=wait_timeout,
                    )
                    transient_error_types = upload_result.transient_error_types
                    return upload_result.source

                service = uploader.create_drive_import_service(
                    add_file=add_downloaded_file,
                )
                async with uploader.get_download_semaphore():
                    source = await service.add_drive_file(
                        value.notebook_id,
                        value.document_id,
                        title=(None if value.wait else value.title),
                        wait=False,
                        wait_timeout=value.wait_timeout,
                    )
        return SourceAddFileResult(
            _source_record(source),
            transient_error_types,
            deferred_title if value.kind is SourceFileInputKind.DRIVE_DOWNLOAD else None,
        )
    except NotebookLMError as exc:
        # ``source.add_file`` stays adapter-owned (D4), so this is the one
        # permanent source-add translation: the Scotty pipeline's post-registration
        # graph (``source_id``/``stage``-tagged transport leaves, the rejected-input
        # ``ValidationError``, the ``SourceAddError`` wrap) becomes neutral evidence
        # here. The pipeline's own legs keep their raw ``httpx`` semantics: only
        # this row boundary translates.
        raise _source_add_failure(exc, Operation.SOURCE_ADD_FILE) from exc


_PROTOCOL_JUSTIFICATION = (
    "Source registration has a tentative-source mobile variant (ADR-0035 principle 2), "
    "so the create/probe/rename sequence stays adapter-owned; gate table §4."
)

SOURCE_ADD_FILE = CustomBinding(
    definition=SOURCE_ADD_FILE_DEF,
    handler=_add_file,
    native=(
        NativeCallSpec.constant(RPCMethod.ADD_SOURCE_FILE, key=_REGISTER),
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SNAPSHOT),
        NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS, key=_LIMITS),
        NativeCallSpec.constant(RPCMethod.UPDATE_SOURCE, key=_RENAME),
    ),
    justification=(
        "File upload is a protocol-specific workflow (Scotty legs plus registration); "
        "gate table §3.13, ADR-0035 principle 2."
    ),
    category="protocol",
    collaborators=(_SOURCE_UPLOADER,),
)


SOURCE_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        SOURCE_LIST.definition.key: SOURCE_LIST,
        SOURCE_GET.definition.key: SOURCE_GET,
        SOURCE_WAIT.definition.key: SOURCE_WAIT,
        SOURCE_DELETE.definition.key: SOURCE_DELETE,
        SOURCE_REFRESH.definition.key: SOURCE_REFRESH,
        SOURCE_CHECK_FRESHNESS.definition.key: SOURCE_CHECK_FRESHNESS,
        SOURCE_GET_GUIDE.definition.key: SOURCE_GET_GUIDE,
        SOURCE_GET_FULLTEXT.definition.key: SOURCE_GET_FULLTEXT,
        SOURCE_ADD_FILE.definition.key: SOURCE_ADD_FILE,
    }
)

__all__ = [
    "SOURCE_ADD_FILE",
    "SOURCE_CHECK_FRESHNESS",
    "SOURCE_DELETE",
    "SOURCE_GET",
    "SOURCE_GET_FULLTEXT",
    "SOURCE_GET_GUIDE",
    "SOURCE_LIST",
    "SOURCE_REFRESH",
    "SOURCE_ROWS",
    "SOURCE_WAIT",
    "upload_backend",
]
