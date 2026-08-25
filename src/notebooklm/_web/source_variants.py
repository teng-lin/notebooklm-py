"""Web workflow bindings for the remaining Source variants."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import urlparse

from .._backend import BackendContractError, BackendError, BackendErrorReason
from .._deadline import RuntimeDeadline
from .._idempotency import _CreateResultKind, _IdempotentCreateResult
from .._operations import Operation
from .._projectors import project_source
from .._records import (
    SourceAddCommitState,
    SourceAddDriveInput,
    SourceAddDriveResult,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTextInput,
    SourceAddTextResult,
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
from .._source.add import (
    SourceAddService,
    honor_requested_title,
    honor_requested_title_if_fresh,
)
from .._source.batch import SourceBatchAddService
from .._types.sources import _SOURCE_TYPE_CODE_MAP, SourceType
from .._url_utils import is_youtube_url
from ..exceptions import (
    NotebookLMError,
    SourceNotFoundError,
    ValidationError,
)
from ..rpc import RPCMethod
from ..rpc.types import drive_source_status_to_str, source_status_to_str
from ..types import Source
from .codec import settings as settings_codec
from .codec.sources import (
    decode_add_source_records,
    decode_file_registration,
    decode_source_record,
    decode_source_snapshot,
    encode_add_drive,
    encode_add_text,
    encode_add_url_batch,
    encode_register_file_source,
    encode_source_snapshot,
    encode_update_source,
)
from .studio_facade import StudioFacadeWebHandlers

source_logger = logging.getLogger("notebooklm").getChild("_sources")


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


class SourceVariantWebHandlers(StudioFacadeWebHandlers):
    """Remaining Source workflows mixed into the composed web backend."""

    _executor: Any
    _source_uploader: Any

    def _capture_public_failure(self, exc: Exception, *, operation: Operation) -> Any:
        raise NotImplementedError

    async def _source_snapshot_records(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        strict: bool = False,
        outcome_unknown_on_expiry: bool = False,
    ) -> tuple[SourceRecord, ...]:
        """Fetch and decode one recency-writing notebook source snapshot."""

        payload = await self._rpc_call(
            RPCMethod.GET_NOTEBOOK,
            encode_source_snapshot(notebook_id),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return decode_source_snapshot(
            notebook_id,
            payload,
            strict=strict,
            logger=source_logger,
        )

    async def _source_public_snapshot(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None = None,
    ) -> list[Source]:
        records = await self._source_snapshot_records(
            notebook_id,
            operation=operation,
            deadline=deadline,
        )
        return [project_source(record) for record in records]

    async def _source_public_get_for_operation(
        self,
        notebook_id: str,
        source_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> Source | None:
        sources = await self._source_public_snapshot(
            notebook_id,
            operation=operation,
            # This is mutation reconciliation, not the facade-owned source
            # poll. It consumes the mutation's absolute semantic budget.
            deadline=deadline,
        )
        return next((source for source in sources if source.id == source_id), None)

    async def _source_select_record(
        self,
        notebook_id: str,
        source_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        outcome_unknown_on_expiry: bool = False,
    ) -> SourceRecord | None:
        """Select one exact-id record from a composite's own snapshot read.

        The ``source.get`` leaf is a codec row since P9.3; this helper exists
        because composites attribute the read to themselves and thread
        ``outcome_unknown_on_expiry`` through it, which a codec row cannot.
        """
        records = await self._source_snapshot_records(
            notebook_id,
            operation=operation,
            deadline=deadline,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return next((source for source in records if source.id == source_id), None)

    async def _create_url_source(
        self,
        notebook_id: str,
        url: str,
        *,
        youtube: bool,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> Source | None:
        payload = await self._rpc_call(
            RPCMethod.ADD_SOURCE,
            encode_add_url_batch(notebook_id, [url], youtube_flags=[youtube]),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            disable_internal_retries=True,
            operation_variant="url",
        )
        records = decode_add_source_records(payload)
        return _first_projected_source(records)

    async def _rename_source_public(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        hydrate_on_null: bool,
    ) -> Source | None:
        payload = await self._rpc_call(
            RPCMethod.UPDATE_SOURCE,
            encode_update_source(source_id, new_title),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if payload:
            return project_source(decode_source_record(payload, method=RPCMethod.UPDATE_SOURCE))
        if not hydrate_on_null:
            return None
        source = await self._source_public_get_for_operation(
            notebook_id,
            source_id,
            operation=operation,
            deadline=deadline,
        )
        if source is None:
            raise SourceNotFoundError(source_id, method_id=RPCMethod.UPDATE_SOURCE.value)
        return source

    async def _source_add_url(
        self,
        value: SourceAddUrlInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceAddUrlResult:
        """Run the live generic/YouTube URL workflow with optional outer budgeting."""
        adder = SourceAddService()

        def extract_youtube_video_id(url: str) -> str | None:
            return adder.extract_youtube_video_id(
                url,
                parse_url=urlparse,
                extract_video_id_from_parsed_url=adder.extract_video_id_from_parsed_url,
                is_valid_video_id=adder.is_valid_video_id,
                logger=source_logger,
            )

        async def rename_source(
            notebook_id: str,
            source_id: str,
            new_title: str,
        ) -> Source | None:
            return await self._rename_source_public(
                notebook_id,
                source_id,
                new_title,
                operation=Operation.SOURCE_ADD_URL,
                deadline=deadline,
                hydrate_on_null=True,
            )

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
                    add_youtube_source=lambda notebook_id, url: self._create_url_source(
                        notebook_id,
                        url,
                        youtube=True,
                        operation=Operation.SOURCE_ADD_URL,
                        deadline=deadline,
                    ),
                    add_url_source=lambda notebook_id, url: self._create_url_source(
                        notebook_id,
                        url,
                        youtube=False,
                        operation=Operation.SOURCE_ADD_URL,
                        deadline=deadline,
                    ),
                    list_sources=lambda notebook_id: self._source_public_snapshot(
                        notebook_id,
                        operation=Operation.SOURCE_ADD_URL,
                        deadline=deadline,
                    ),
                    wait_until_ready=_facade_owned_wait,
                    extract_youtube_video_id=extract_youtube_video_id,
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
                        "source_add_failure": self._capture_public_failure(
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

    async def _source_add_url_batch(
        self,
        value: SourceAddUrlBatchInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceAddUrlBatchResult:
        """Run one non-replayed true-batch URL write and preserve positions."""
        adder = SourceAddService()

        def extract_youtube_video_id(url: str) -> str | None:
            return adder.extract_youtube_video_id(
                url,
                parse_url=urlparse,
                extract_video_id_from_parsed_url=adder.extract_video_id_from_parsed_url,
                is_valid_video_id=adder.is_valid_video_id,
                logger=source_logger,
            )

        async def create_sources(
            notebook_id: str,
            urls: Sequence[str],
            youtube_flags: Sequence[bool],
        ) -> list[Source]:
            payload = await self._rpc_call(
                RPCMethod.ADD_SOURCE,
                encode_add_url_batch(
                    notebook_id,
                    list(urls),
                    youtube_flags=list(youtube_flags),
                ),
                operation=Operation.SOURCE_ADD_URL_BATCH,
                deadline=deadline,
                source_path=f"/notebook/{notebook_id}",
                disable_internal_retries=True,
                operation_variant="url",
            )
            return [project_source(record) for record in decode_add_source_records(payload)]

        async def list_sources(notebook_id: str, **kwargs: Any) -> list[Source]:
            sources = await self._source_public_snapshot(
                notebook_id,
                operation=Operation.SOURCE_ADD_URL_BATCH,
                deadline=deadline,
            )
            statuses = kwargs.get("statuses")
            return (
                sources
                if statuses is None
                else [source for source in sources if source.status in statuses]
            )

        outcomes = await SourceBatchAddService().add_urls(
            value.notebook_id,
            value.urls,
            create_sources=create_sources,
            list_sources=list_sources,
            extract_youtube_video_id=extract_youtube_video_id,
            logger=source_logger,
        )
        return SourceAddUrlBatchResult(
            tuple(
                SourceUrlBatchItemRecord(
                    url=item.url,
                    source=(_source_record(item.source) if item.source is not None else None),
                    error=(
                        self._capture_public_failure(
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

    async def _source_add_text(
        self,
        value: SourceAddTextInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceAddTextResult:
        async def create_source(
            notebook_id: str,
            title: str,
            content: str,
        ) -> Source | None:
            payload = await self._rpc_call(
                RPCMethod.ADD_SOURCE,
                encode_add_text(notebook_id, title, content),
                operation=Operation.SOURCE_ADD_TEXT,
                deadline=deadline,
                source_path=f"/notebook/{notebook_id}",
                operation_variant="text",
            )
            records = decode_add_source_records(payload) if payload is not None else ()
            return _first_projected_source(records)

        source = await SourceAddService().add_text(
            value.notebook_id,
            value.title,
            value.content,
            wait=False,
            wait_timeout=value.wait_timeout,
            idempotent=value.idempotent,
            create_source=create_source,
            wait_until_ready=_facade_owned_wait,
            logger=source_logger,
        )
        return SourceAddTextResult(_source_record(source))

    async def _source_add_drive(
        self,
        value: SourceAddDriveInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceAddDriveResult:
        adder = SourceAddService()

        async def create_source(
            notebook_id: str,
            file_id: str,
            title: str,
            mime_type: str,
        ) -> Source | None:
            payload = await self._rpc_call(
                RPCMethod.ADD_SOURCE,
                encode_add_drive(notebook_id, file_id, title, mime_type),
                operation=Operation.SOURCE_ADD_DRIVE,
                deadline=deadline,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                disable_internal_retries=True,
                operation_variant="drive",
            )
            records = decode_add_source_records(payload) if payload is not None else ()
            return _first_projected_source(records)

        async def rename_source(
            notebook_id: str,
            source_id: str,
            new_title: str,
        ) -> Source | None:
            return await self._rename_source_public(
                notebook_id,
                source_id,
                new_title,
                operation=Operation.SOURCE_ADD_DRIVE,
                deadline=deadline,
                hydrate_on_null=False,
            )

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
                list_sources=lambda notebook_id: self._source_public_snapshot(
                    notebook_id,
                    operation=Operation.SOURCE_ADD_DRIVE,
                    deadline=deadline,
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

    async def _source_register_file(
        self,
        notebook_id: str,
        filename: str,
    ) -> SourceFileRegistrationRecord:
        payload = await self._rpc_call(
            RPCMethod.ADD_SOURCE_FILE,
            encode_register_file_source(filename, notebook_id),
            operation=Operation.SOURCE_ADD_FILE,
            deadline=None,
            source_path=f"/notebook/{notebook_id}",
            allow_null=False,
            disable_internal_retries=True,
        )
        return decode_file_registration(payload, filename=filename)

    async def _source_upload_list(self, notebook_id: str) -> list[Source]:
        return await self._source_public_snapshot(
            notebook_id,
            operation=Operation.SOURCE_ADD_FILE,
            deadline=None,
        )

    async def _source_upload_rename(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
    ) -> Source | None:
        return await self._rename_source_public(
            notebook_id,
            source_id,
            new_title,
            operation=Operation.SOURCE_ADD_FILE,
            deadline=None,
            hydrate_on_null=False,
        )

    def _require_source_uploader(self) -> Any:
        if self._source_uploader is None:
            raise BackendContractError(
                "source.add_file requires the composition-root upload pipeline",
                operation=Operation.SOURCE_ADD_FILE,
            )
        return self._source_uploader

    async def _source_add_file(
        self,
        value: SourceAddFileInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceAddFileResult:
        del deadline  # upload/session timeouts retain their existing independent windows
        uploader = self._require_source_uploader()
        if value.finalize_source is not None:
            source = await honor_requested_title(
                self._source_upload_rename,
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

            deferred_title: str | None = None

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

    async def _source_file_limit(self) -> int | None:
        result = await self._rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            settings_codec.encode_get_user_settings(),
            operation=Operation.SOURCE_ADD_FILE,
            deadline=None,
            source_path="/",
        )
        return settings_codec.decode_account_limits(result).source_limit
