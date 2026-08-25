"""Source read and single-native source mutation codec rows (P9.3 source domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``SOURCE_LIST``/``SOURCE_GET``/``SOURCE_WAIT`` share the recency-writing
``GET_NOTEBOOK`` snapshot; ``SOURCE_GET`` selects its exact id inside ``decode``
and ``SOURCE_WAIT`` is the one ``DeadlineMode.IGNORE`` row (source polling
historically never clamps an in-flight read).

The remaining source-add rows (``SOURCE_ADD_URL``, ``SOURCE_ADD_URL_BATCH``,
``SOURCE_ADD_DRIVE``, ``SOURCE_ADD_FILE``) are
:class:`CustomBinding` rows (P9.4b): each declares exactly the natives the
policy ledger lists under spec keys (``snapshot``, ``create``, ``rename``,
``register``, ``limits``) and sequences them through the row-scoped invoker
with the same options the P6.7 handlers set.  All four are *protocol* rows —
source registration has a tentative-source mobile variant (ADR-0035
principle 2).  ``source.add_text`` no longer has a row at all: P10 R3.2 hoisted
its workflow into ``SourceService`` over the ``SOURCE_REGISTER`` primitive, so
the family's one *compatibility* row is gone.  All four translate (P10
invariant I8): the
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
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urlparse

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
from ..._idempotency import _CreateResultKind, _IdempotentCreateResult
from ..._operations import Operation
from ..._projectors import project_source
from ..._records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_ADD_URL_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_WAIT_DEF,
    SourceAddCommitState,
    SourceAddDriveInput,
    SourceAddDriveResult,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTitleState,
    SourceAddUrlBatchInput,
    SourceAddUrlBatchResult,
    SourceAddUrlInput,
    SourceAddUrlReceipt,
    SourceAddUrlResult,
    SourceFileInputKind,
    SourceFileRegistrationRecord,
    SourceRecord,
    SourceUrlBatchItemRecord,
)
from ..._source.add import (
    SourceAddService,
    honor_requested_title,
    honor_requested_title_if_fresh,
)
from ..._source.batch import SourceBatchAddService
from ..._source_upload_port import SourceUploadBackend
from ..._types.sources import _SOURCE_TYPE_CODE_MAP, SourceType
from ..._url_utils import is_youtube_url
from ...exceptions import NotebookLMError, ValidationError
from ...rpc import RPCMethod
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
_CREATE = "create"
_RENAME = "rename"
_REGISTER = "register"
_LIMITS = "limits"
_CAPTURE_PUBLIC_FAILURE = "capture_public_failure"
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
    ``capture_public_failure`` row collaborator (as ``_add_url`` and
    ``_add_url_batch``'s per-item captures still do): that collaborator leaves
    ``ROW_COLLABORATOR_NAMES`` with the last source-add hoist, and
    ``SOURCE_ADD_FILE`` — which needs this — is permanent under D4.  Both reach
    the same ``_web.failure_projection`` function; the collaborator seam buys a
    custom row nothing that a sibling ``_web`` import does not already give it.
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


def _first_projected_source(records: Sequence[SourceRecord]) -> Source | None:
    """Project the first decoded create row without retaining positional wire reads."""
    record = next(iter(records), None)
    return project_source(record) if record is not None else None


async def _facade_owned_wait(*_args: Any, **_kwargs: Any) -> Source:
    """Fail closed if an adapter create path tries to take over readiness polling."""
    raise BackendContractError(
        "source readiness polling belongs to the public source facade",
        operation=Operation.SOURCE_WAIT,
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


def _youtube_video_id_extractor(adder: SourceAddService) -> Any:
    def extract_youtube_video_id(url: str) -> str | None:
        return adder.extract_youtube_video_id(
            url,
            parse_url=urlparse,
            extract_video_id_from_parsed_url=adder.extract_video_id_from_parsed_url,
            is_valid_video_id=adder.is_valid_video_id,
            logger=source_logger,
        )

    return extract_youtube_video_id


async def _add_url(
    value: SourceAddUrlInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SourceAddUrlResult:
    """Run the live generic/YouTube URL workflow with optional outer budgeting."""
    adder = SourceAddService()
    capture_public_failure = invoke.collaborator(_CAPTURE_PUBLIC_FAILURE)

    async def rename_source(notebook_id: str, source_id: str, new_title: str) -> Source | None:
        return await _rename_source(
            invoke,
            notebook_id,
            source_id,
            new_title,
            deadline=deadline,
            hydrate_on_null=True,
        )

    async def create_url_source(notebook_id: str, url: str, *, youtube: bool) -> Source | None:
        payload = await invoke.call(
            _CREATE,
            sources_codec.encode_add_url_payload(notebook_id, [url], youtube_flags=[youtube]),
            deadline=deadline,
            disable_internal_retries=True,
        )
        return _first_projected_source(sources_codec.decode_add_source_records(payload))

    if value.finalize_source is not None:
        source_before_title = project_source(value.finalize_source)
        source = await honor_requested_title(
            rename_source,
            value.notebook_id,
            source_before_title,
            value.requested_title,
            source_logger,
        )
        normalized_title = (
            value.requested_title.strip() if value.requested_title is not None else ""
        )
        return SourceAddUrlResult(
            _source_record(source),
            SourceAddUrlReceipt(
                SourceAddCommitState.CREATED,
                (
                    SourceAddTitleState.RENAMED
                    if normalized_title and source.title == normalized_title
                    else SourceAddTitleState.RENAME_FAILED
                ),
            ),
        )

    try:
        create_result = cast(
            _IdempotentCreateResult[Source],
            await adder.add_url(
                value.notebook_id,
                value.url,
                wait=False,
                wait_timeout=value.wait_timeout,
                add_youtube_source=lambda notebook_id, url: create_url_source(
                    notebook_id, url, youtube=True
                ),
                add_url_source=lambda notebook_id, url: create_url_source(
                    notebook_id, url, youtube=False
                ),
                list_sources=lambda notebook_id: _snapshot_sources(
                    invoke, notebook_id, deadline=deadline
                ),
                wait_until_ready=_facade_owned_wait,
                extract_youtube_video_id=_youtube_video_id_extractor(adder),
                is_youtube_url=is_youtube_url,
                logger=source_logger,
                return_result=True,
            ),
        )
    except NotebookLMError as exc:
        outcome_unknown = bool(getattr(exc, "unconfirmed", False))
        receipt = SourceAddUrlReceipt(
            commit_state=(
                SourceAddCommitState.UNKNOWN if outcome_unknown else SourceAddCommitState.FAILED
            ),
            title_state=SourceAddTitleState.NOT_ATTEMPTED,
            outcome_unknown=outcome_unknown,
        )
        raise BackendError(
            message=str(exc.args[0]) if exc.args else "",
            operation=Operation.SOURCE_ADD_URL,
            outcome_unknown=outcome_unknown,
            diagnostics=MappingProxyType(
                {
                    "receipt": receipt,
                    "source_add_failure": capture_public_failure(
                        exc,
                        operation=Operation.SOURCE_ADD_URL,
                    ),
                }
            ),
            reason=BackendErrorReason.SOURCE_ADD,
        ) from exc

    source_before_title = create_result.value
    requested_title = value.requested_title
    normalized_title = requested_title.strip() if requested_title is not None else ""
    source = (
        source_before_title
        if value.wait
        else await honor_requested_title_if_fresh(
            rename_source,
            value.notebook_id,
            create_result,
            requested_title,
            source_logger,
            probe_proves_freshness=True,
        )
    )
    if not normalized_title:
        title_state = SourceAddTitleState.NOT_REQUESTED
    elif source_before_title.title == normalized_title:
        title_state = SourceAddTitleState.UNCHANGED
    elif value.wait:
        title_state = SourceAddTitleState.NOT_ATTEMPTED
    elif source.title == normalized_title:
        title_state = SourceAddTitleState.RENAMED
    else:
        title_state = SourceAddTitleState.RENAME_FAILED

    return SourceAddUrlResult(
        source=_source_record(source),
        receipt=SourceAddUrlReceipt(
            commit_state=(
                SourceAddCommitState.CREATED
                if create_result.kind is _CreateResultKind.CREATED
                else SourceAddCommitState.RECONCILED
            ),
            title_state=title_state,
        ),
    )


async def _add_url_batch(
    value: SourceAddUrlBatchInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SourceAddUrlBatchResult:
    """Run one non-replayed true-batch URL write and preserve positions."""
    adder = SourceAddService()
    capture_public_failure = invoke.collaborator(_CAPTURE_PUBLIC_FAILURE)

    async def create_sources(
        notebook_id: str,
        urls: Sequence[str],
        youtube_flags: Sequence[bool],
    ) -> list[Source]:
        payload = await invoke.call(
            _CREATE,
            sources_codec.encode_add_url_payload(notebook_id, urls, youtube_flags=youtube_flags),
            deadline=deadline,
            disable_internal_retries=True,
        )
        return [
            project_source(record) for record in sources_codec.decode_add_source_records(payload)
        ]

    async def list_sources(notebook_id: str, **kwargs: Any) -> list[Source]:
        sources = await _snapshot_sources(invoke, notebook_id, deadline=deadline)
        statuses = kwargs.get("statuses")
        return (
            sources
            if statuses is None
            else [source for source in sources if source.status in statuses]
        )

    try:
        outcomes = await SourceBatchAddService().add_urls(
            value.notebook_id,
            value.urls,
            create_sources=create_sources,
            list_sources=list_sources,
            extract_youtube_video_id=_youtube_video_id_extractor(adder),
            logger=source_logger,
        )
    except NotebookLMError as exc:
        # The batch service marks the whole write unconfirmed and re-raises the
        # native failure with a rewritten message; the row carries that leaf as
        # neutral evidence instead of letting it escape the port.
        raise _source_add_failure(exc, Operation.SOURCE_ADD_URL_BATCH) from exc
    return SourceAddUrlBatchResult(
        tuple(
            SourceUrlBatchItemRecord(
                url=item.url,
                source=(_source_record(item.source) if item.source is not None else None),
                error=(
                    capture_public_failure(
                        item.error,
                        operation=Operation.SOURCE_ADD_URL_BATCH,
                    )
                    if item.error is not None
                    else None
                ),
            )
            for item in outcomes
        )
    )


async def _add_drive(
    value: SourceAddDriveInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> SourceAddDriveResult:
    adder = SourceAddService()

    async def create_source(
        notebook_id: str,
        file_id: str,
        title: str,
        mime_type: str,
    ) -> Source | None:
        payload = await invoke.call(
            _CREATE,
            sources_codec.encode_add_drive_payload(notebook_id, file_id, title, mime_type),
            deadline=deadline,
            disable_internal_retries=True,
        )
        records = sources_codec.decode_add_source_records(payload) if payload is not None else ()
        return _first_projected_source(records)

    async def rename_source(notebook_id: str, source_id: str, new_title: str) -> Source | None:
        return await _rename_source(
            invoke,
            notebook_id,
            source_id,
            new_title,
            deadline=deadline,
            hydrate_on_null=False,
        )

    try:
        if value.finalize_source is not None:
            source = await honor_requested_title(
                rename_source,
                value.notebook_id,
                project_source(value.finalize_source),
                value.title,
                source_logger,
            )
            return SourceAddDriveResult(_source_record(source))

        result = cast(
            _IdempotentCreateResult[Source],
            await adder.add_drive(
                value.notebook_id,
                value.file_id,
                value.title,
                mime_type=value.mime_type,
                wait=False,
                wait_timeout=value.wait_timeout,
                create_source=create_source,
                list_sources=lambda notebook_id: _snapshot_sources(
                    invoke, notebook_id, deadline=deadline
                ),
                wait_until_ready=_facade_owned_wait,
                logger=source_logger,
                return_result=True,
            ),
        )

        source = (
            result.value
            if value.wait
            else await honor_requested_title_if_fresh(
                rename_source,
                value.notebook_id,
                result,
                value.title,
                source_logger,
                probe_proves_freshness=True,
            )
        )
        return SourceAddDriveResult(_source_record(source))
    except NotebookLMError as exc:
        # Rejected input, the ``SourceAddError`` wrap and the unconfirmed transport
        # four-tuple all leave as neutral evidence under one reason.
        raise _source_add_failure(exc, Operation.SOURCE_ADD_DRIVE) from exc


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
            source = await honor_requested_title(
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

SOURCE_ADD_URL = CustomBinding(
    definition=SOURCE_ADD_URL_DEF,
    handler=_add_url,
    native=(
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SNAPSHOT),
        NativeCallSpec.constant(RPCMethod.ADD_SOURCE, "url", key=_CREATE),
        NativeCallSpec.constant(RPCMethod.UPDATE_SOURCE, key=_RENAME),
    ),
    justification=_PROTOCOL_JUSTIFICATION,
    category="protocol",
    collaborators=(_CAPTURE_PUBLIC_FAILURE,),
)

SOURCE_ADD_URL_BATCH = CustomBinding(
    definition=SOURCE_ADD_URL_BATCH_DEF,
    handler=_add_url_batch,
    native=(
        NativeCallSpec.constant(RPCMethod.ADD_SOURCE, "url", key=_CREATE),
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SNAPSHOT),
    ),
    justification=_PROTOCOL_JUSTIFICATION,
    category="protocol",
    collaborators=(_CAPTURE_PUBLIC_FAILURE,),
)

SOURCE_ADD_DRIVE = CustomBinding(
    definition=SOURCE_ADD_DRIVE_DEF,
    handler=_add_drive,
    native=(
        NativeCallSpec.constant(RPCMethod.ADD_SOURCE, "drive", key=_CREATE),
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_SNAPSHOT),
        NativeCallSpec.constant(RPCMethod.UPDATE_SOURCE, key=_RENAME),
    ),
    justification=_PROTOCOL_JUSTIFICATION,
    category="protocol",
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
        SOURCE_ADD_URL.definition.key: SOURCE_ADD_URL,
        SOURCE_ADD_URL_BATCH.definition.key: SOURCE_ADD_URL_BATCH,
        SOURCE_ADD_DRIVE.definition.key: SOURCE_ADD_DRIVE,
        SOURCE_ADD_FILE.definition.key: SOURCE_ADD_FILE,
    }
)

__all__ = [
    "SOURCE_ADD_DRIVE",
    "SOURCE_ADD_FILE",
    "SOURCE_ADD_URL",
    "SOURCE_ADD_URL_BATCH",
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
