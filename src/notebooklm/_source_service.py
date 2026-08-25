"""Transport-neutral semantic service for the migrated P6.7 Source slice."""

from __future__ import annotations

import logging
from pathlib import Path
from types import MappingProxyType

from ._backend import (
    BackendAdapter,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._operations import Operation
from ._records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_TEXT_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_REGISTER_DEF,
    SOURCE_UPDATE_DEF,
    SOURCE_WAIT_DEF,
    SourceAddDriveInput,
    SourceAddDriveResult,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTextResult,
    SourceAddUrlBatchInput,
    SourceAddUrlBatchResult,
    SourceDeleteInput,
    SourceFileInputKind,
    SourceFreshnessInput,
    SourceFulltextInput,
    SourceFulltextResult,
    SourceGetInput,
    SourceGuideInput,
    SourceGuideResult,
    SourcePatchTitleInput,
    SourceProgressCallback,
    SourceRecord,
    SourceRefreshInput,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceUpdateResult,
    SourceWaitSnapshotInput,
    SourceWaitSnapshotResult,
)

# The pre-P10 ``source.add_text`` row reached this message through
# ``SourceAddService``; the workflow that replaces the row owns it verbatim so
# the public ``NonIdempotentRetryError`` text is unchanged.
_TEXT_NON_IDEMPOTENT_MESSAGE = (
    "add_text cannot be marked idempotent: text sources have no "
    "reliable server-side dedupe key (titles non-unique, content "
    "not exposed). For idempotent text imports, embed a UUID in "
    "the title and dedupe client-side. See "
    "docs/python-api.md#idempotency."
)

#: Failure kinds the pre-P10 handler's ``except RPCError`` wrapped into a
#: ``SourceAddError``. It is deliberately *not* "every RPC-shaped reason":
#: ``AuthError``/``RateLimitError``/``ServerError``/``NetworkError`` (and
#: ``RPCTimeoutError``, a ``NetworkError`` subclass) were caught first and
#: re-raised unwrapped under ADR-0019, so callers can still act on the specific
#: type. Anything outside this set keeps the leaf's own public identity.
_TEXT_WRAPPED_FAILURE_KINDS = frozenset(
    {
        SourceAddFailureKind.RPC,
        SourceAddFailureKind.CLIENT,
        SourceAddFailureKind.DECODING,
        SourceAddFailureKind.RESPONSE_TOO_LARGE,
        SourceAddFailureKind.UNKNOWN_RPC_METHOD,
    }
)

# The same logger name and level the retired row logged under.
_source_logger = logging.getLogger("notebooklm").getChild("_sources")


def _source_add_failure(
    operation: Operation,
    record: SourceAddFailureRecord,
    *,
    outcome_unknown: bool = False,
    dispatched: bool = False,
) -> BackendError:
    """Report one source-add failure as bounded neutral evidence.

    ``_backend_compat`` replays an *equal* public exception at the facade from
    ``record`` alone, so a transport-neutral workflow never has to name — or
    construct — a public exception type.
    """
    return BackendError(
        message=record.message,
        operation=operation,
        outcome_unknown=outcome_unknown,
        diagnostics=MappingProxyType({"source_add_failure": record}),
        reason=BackendErrorReason.SOURCE_ADD,
        dispatched=dispatched,
    )


def _leaf_failure_record(error: BackendError) -> SourceAddFailureRecord | None:
    """Return the leaf's captured public graph, if the backend captured one.

    Capturing it is a *web* convention, not a port requirement: another adapter
    may report a closed reason and nothing else, and the compatibility projector
    reconstructs a public exception from the reason alone in that case. ``None``
    therefore means "project by reason", not "malformed". A value of the wrong
    type is malformed, and fails closed.
    """
    record = (error.diagnostics or {}).get("public_error_failure")
    if record is None:
        return None
    if not isinstance(record, SourceAddFailureRecord):
        raise BackendContractError(
            "source registration failure has invalid public-error evidence",
            operation=error.operation,
        ) from error
    return record


class SourceService:
    """Invoke typed Source operations without web/RPC/public-model vocabulary."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        # P9.2 contract 3: a service-owned workflow mints one deadline
        # before its first leaf and threads that identity through every phase.
        self._deadline_factory = deadline_factory

    async def add_urls_batch(
        self,
        notebook_id: str,
        urls: tuple[str, ...],
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddUrlBatchResult:
        return await self._backend.invoke(
            SOURCE_ADD_URL_BATCH_DEF,
            SourceAddUrlBatchInput(notebook_id, urls),
            deadline=deadline,
        )

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        *,
        wait: bool,
        wait_timeout: float,
        idempotent: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddTextResult:
        """Register one pasted-text source over the ``source.register`` leaf.

        Text is the source-add family's one registration with no probe: titles
        are not unique and the body is never echoed, so there is nothing to
        reconcile against and no baseline worth taking. The workflow is
        therefore the refusal, one write, and the two ways that write can fail
        to name a source — which is exactly what the retired row did.

        ``wait``/``wait_timeout`` stay on the signature because the input record
        carries them, but readiness polling is the public facade's (the row
        failed closed on any attempt to take it below the port).

        No deadline is minted from ``_deadline_factory``. ``source.add_text`` is
        deliberately absent from the closed deadline ledger
        (``_web/deadlines.py``): it is one write with no aggregate budget, and
        the retired row ran it with whatever the caller supplied — which the
        facade leaves ``None``. Starting one here would invent a budget, and
        with it an expiry path that turns a timeout on a
        ``NON_IDEMPOTENT_NO_RETRY`` create into a different public failure.
        """
        workflow = SOURCE_ADD_TEXT_DEF.key
        if idempotent:
            # Checked before the leaf gate: refusing a replay writes nothing,
            # and the pre-P10 handler refused before it looked at anything else.
            raise _source_add_failure(
                workflow,
                SourceAddFailureRecord(
                    kind=SourceAddFailureKind.NON_IDEMPOTENT_RETRY,
                    message=_TEXT_NON_IDEMPOTENT_MESSAGE,
                    args=(_TEXT_NON_IDEMPOTENT_MESSAGE,),
                ),
            )
        require_leaves(self._backend, SOURCE_REGISTER_DEF.key)

        _source_logger.debug("Adding text source to notebook %s: %s", notebook_id, title)
        try:
            registered = await self._backend.invoke(
                SOURCE_REGISTER_DEF,
                SourceRegisterInput(
                    notebook_id,
                    SourceRegisterKind.TEXT,
                    title=title,
                    content=content,
                ),
                deadline=deadline,
            )
        except BackendError as error:
            raise self._text_registration_failure(workflow, title, error) from error.__cause__

        source = next(iter(registered.sources), None)
        if source is None:
            raise _source_add_failure(
                workflow,
                SourceAddFailureRecord(
                    kind=SourceAddFailureKind.SOURCE_ADD,
                    message=f"API returned no data for text source: {title}",
                    args=(f"API returned no data for text source: {title}",),
                    url=title,
                ),
            )
        return SourceAddTextResult(source)

    @staticmethod
    def _text_registration_failure(
        workflow: Operation,
        title: str,
        error: BackendError,
    ) -> BackendError:
        """Rebind one registration failure to the workflow, wrapping only the RPC leaf.

        The leaf already captured its own public graph as neutral evidence, so
        the wrap is a record nesting a record: no public exception is built,
        yet ``_backend_compat`` reconstructs the identical
        ``SourceAddError(title, cause=<the RPC error>) from <the RPC error>``
        the retired row produced.
        """
        if isinstance(error, BackendContractError) or error.reason is None:
            return error
        if isinstance(error, BackendDeadlineExceededError):
            # An explicitly supplied budget running out is the caller's answer,
            # not a source-add failure: keep the subclass and only re-attribute
            # it, exactly as ``update`` does.
            return rebind_operation(error, workflow)
        leaf = _leaf_failure_record(error)
        if leaf is None:
            # Nothing to wrap and nothing to replay: re-attribute the reason and
            # let the compatibility projector build the public exception from it.
            return rebind_operation(error, workflow)
        if leaf.kind not in _TEXT_WRAPPED_FAILURE_KINDS:
            # ADR-0019 catch ordering: the transport four-tuple and every other
            # public leaf keep their own type, message and fields.
            return _source_add_failure(
                workflow,
                leaf,
                outcome_unknown=error.outcome_unknown,
                dispatched=error.dispatched,
            )
        message = f"Failed to add text source '{title}'"
        return _source_add_failure(
            workflow,
            SourceAddFailureRecord(
                kind=SourceAddFailureKind.SOURCE_ADD,
                message=message,
                args=(message,),
                url=title,
                cause=leaf,
                # ``raise SourceAddError(...) from e`` inside ``except RPCError
                # as e``: the RPC error is the explicit cause, the implicit
                # context, and the reason the context is suppressed.
                context_is_cause=True,
                explicit_cause=True,
                suppress_context=True,
            ),
        )

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str,
        wait: bool,
        wait_timeout: float,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddDriveResult:
        return await self._backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(
                notebook_id,
                file_id,
                title,
                mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
            ),
            deadline=deadline,
        )

    async def finalize_drive_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddDriveResult:
        """Apply a waited Drive title under the original add operation."""

        return await self._backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(
                notebook_id,
                "",
                requested_title,
                "application/vnd.google-apps.document",
                finalize_source=source,
            ),
            deadline=None,
        )

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        *,
        mime_type: str | None,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: SourceProgressCallback | None,
    ) -> SourceAddFileResult:
        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.LOCAL,
                file_path=file_path,
                mime_type=mime_type,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
                on_progress=on_progress,
            ),
            deadline=None,
        )

    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None,
        wait: bool,
        wait_timeout: float,
    ) -> SourceAddFileResult:
        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.DRIVE_DOWNLOAD,
                document_id=document_id,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
            ),
            deadline=None,
        )

    async def finalize_file_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddFileResult:
        """Apply a waited upload title under the original add operation."""

        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.LOCAL,
                title=requested_title,
                finalize_source=source,
            ),
            deadline=None,
        )

    async def delete(self, notebook_id: str, source_id: str) -> None:
        await self._backend.invoke(
            SOURCE_DELETE_DEF,
            SourceDeleteInput(notebook_id, source_id),
            deadline=None,
        )

    async def update(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceUpdateResult:
        """Rename through one patch leaf and hydrate only a null echo."""
        workflow = SOURCE_UPDATE_DEF.key
        # The conditional leaf set is still checked up front: a later backend
        # must never perform the title write and only then discover it cannot
        # execute the null-echo hydration path.
        require_leaves(self._backend, SOURCE_PATCH_TITLE_DEF.key, SOURCE_GET_DEF.key)
        if deadline is None and self._deadline_factory is not None:
            deadline = self._deadline_factory.start()

        write_dispatched = False
        try:
            patched = await self._backend.invoke(
                SOURCE_PATCH_TITLE_DEF,
                SourcePatchTitleInput(notebook_id, source_id, new_title),
                deadline=deadline,
            )
            write_dispatched = True
            source = patched.source
            if source is None:
                hydrated = await self._backend.invoke(
                    SOURCE_GET_DEF,
                    SourceGetInput(notebook_id, source_id),
                    deadline=deadline,
                )
                source = hydrated.source
                if source is None:
                    raise BackendError(
                        message=f"Source not found: {source_id}",
                        operation=workflow,
                        diagnostics=MappingProxyType(
                            {
                                "source_id": source_id,
                                "raw_response": None,
                            }
                        ),
                        reason=BackendErrorReason.SOURCE_NOT_FOUND,
                    )
            return SourceUpdateResult(source if return_object else None)
        except BackendError as error:
            if error.operation is workflow:
                raise
            if write_dispatched and isinstance(error, BackendDeadlineExceededError):
                # The patch landed but the hydration read exhausted the shared
                # budget: the workflow outcome is now unsafe to retry blindly.
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from error.__cause__

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        await self._backend.invoke(
            SOURCE_REFRESH_DEF,
            SourceRefreshInput(notebook_id, source_id),
            deadline=None,
        )

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        result = await self._backend.invoke(
            SOURCE_CHECK_FRESHNESS_DEF,
            SourceFreshnessInput(notebook_id, source_id),
            deadline=None,
        )
        return result.fresh

    async def wait_snapshot(self, notebook_id: str) -> SourceWaitSnapshotResult:
        """Fetch one neutral snapshot for a facade-owned source poll tick."""
        return await self._backend.invoke(
            SOURCE_WAIT_DEF,
            SourceWaitSnapshotInput(notebook_id),
            # Source wait historically owns a relative polling budget and does
            # not clamp an in-flight GET_NOTEBOOK read to the remaining time.
            deadline=None,
        )

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuideResult:
        return await self._backend.invoke(
            SOURCE_GET_GUIDE_DEF,
            SourceGuideInput(notebook_id, source_id),
            deadline=None,
        )

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: str,
    ) -> SourceFulltextResult:
        return await self._backend.invoke(
            SOURCE_GET_FULLTEXT_DEF,
            SourceFulltextInput(notebook_id, source_id, output_format),
            deadline=None,
        )


__all__ = ["SourceService"]
