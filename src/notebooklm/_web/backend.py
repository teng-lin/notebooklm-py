"""Web implementation of the private semantic backend port.

P1 assembles this backend. P2.1 routes four notebook/source reads through it;
P2.2 routes three notebook mutation handlers; P2.3 routes the live URL/YouTube
source composite; P5.1–P5.8 route Studio workflows; P6.1 routes Chat; P6.2 routes Research;
P6.3 routes note/mind-map workflows; P6.4 routes labels/collections; P6.5 routes Sharing;
P6.6 routes settings/suggestions; and P6.7 adds the remaining source variants. New or
migrated wire shapes belong in ``_web.codec``; the backend owns their execution authority.
"""

from __future__ import annotations

import asyncio
import logging
import reprlib
from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import httpx

from .._artifact.payloads import (
    build_interactive_mind_map_artifact_params,
    build_mind_map_params,
)
from .._backend import (
    BackendCapabilities,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
    mark_backend_outcome_unknown,
)
from .._binding import (
    Binding,
    BindingAuditError,
    BindingTable,
    CustomBinding,
    ErrorMode,
    OperationDisposition,
    ResolvedHandlerBinding,
    audit_bindings,
    invoke_binding,
    row_invoker,
)
from .._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from .._env import get_default_language
from .._idempotency import (
    idempotent_create,
    mark_unconfirmed,
    transport_may_have_committed,
)
from .._mind_map import NoteBackedMindMapService
from .._note_service import LegacyNoteBackedService
from .._notebook_payloads import (
    build_create_notebook_params,
    build_get_notebook_params,
    build_update_notebook_params,
)
from .._operations import CallPolicy, Operation, OperationDef
from .._records import (
    ArtifactGetInput,
    ArtifactGetResult,
    ArtifactListInput,
    ArtifactListResult,
    ArtifactRecord,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateNoteInput,
    MindMapGenerateNoteResult,
    NotebookCreateInput,
    NotebookCreateResult,
    NotebookListResult,
    NotebookRecord,
    NotebookUpdateInput,
    NotebookUpdateResult,
    SourceAddFailureRecord,
)
from .._row_adapters.artifacts import (
    unwrap_artifact_rows,
)
from .._runtime.config import assert_resolved_read_timeout
from .._web_cookie_provider import WebCookieProvider, WebCookieSession
from ..exceptions import (
    AuthError,
    ChatError,
    ClientError,
    DecodingError,
    IdempotencyVariantError,
    NetworkError,
    NotebookLMError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)
from ..rpc import (
    ARTIFACT_STATUS_SUGGESTED_WIRE_NAME,
    GrpcStatusCode,
    RPCMethod,
    normalize_grpc_status,
    safe_index,
)
from ..types import ClientMetricsSnapshot
from .bindings.sources import upload_backend
from .chat import ChatWebHandlers
from .codec import settings as settings_codec
from .codec.artifacts import decode_artifact, decode_mind_map_artifact
from .codec.mind_maps import (
    decode_created_interactive_id,
    decode_generated_tree,
)
from .codec.notebooks import (
    decode_notebook,
    decode_notebook_list_result,
    encode_list_notebooks,
)
from .deadline_rpc import DeadlineRpcCaller
from .deadlines import CLIENT_TIMEOUT_DEADLINE_OPERATIONS
from .errors import error_diagnostics, translate_web_error
from .failure_projection import _capture_public_failure
from .registry import WEB_OPERATION_REGISTRY, WEB_SUPPORTED_OPERATIONS, WebOperationBinding
from .runtime import WebExecutionRuntime
from .transport import WebRequest, WebTransport

if TYPE_CHECKING:
    from .._chat import ChatAPI
    from .._client_metrics import ClientMetrics
    from .._reqid_counter import ReqidCounter
    from .._runtime.pipeline import RuntimePipeline
    from .._runtime.transport import RuntimeTransport
    from .._source.upload import SourceUploadPipeline
    from .._transport_drain import TransportDrainTracker

notebook_logger = logging.getLogger("notebooklm._notebooks")
source_logger = logging.getLogger("notebooklm").getChild("_sources")
artifact_logger = logging.getLogger("notebooklm._artifact.listing")

_CREATE_NOTEBOOK_QUOTA_RPC_CODE = 3

# Handler-backed composites whose established public leaves ``invoke()`` re-raises
# unchanged. Empty since the source-add family became ``ErrorMode.RAW_PASSTHROUGH``
# custom rows (P9.4b); kept so the head's projection stays row-metadata-driven.
_RAW_PASSTHROUGH_HANDLER_OPERATIONS: frozenset[Operation] = frozenset()

#: The closed set of collaborator names the head supplies to custom rows. A row
#: declaring any other name is rejected by the construction-time audit.
ROW_COLLABORATOR_NAMES: frozenset[str] = frozenset(
    {"source_uploader", "deadline_factory", "capture_public_failure"}
)


def _row_error_projection(row: Binding | None, operation: Operation) -> tuple[bool, bool | None]:
    """Return ``(raw_passthrough, scrub_request_urls)`` for one operation's failures.

    A custom row carries its own projection as ``error_mode`` metadata:
    ``RAW_PASSTHROUGH`` re-raises the native exception unchanged,
    ``TRANSLATE_SCRUBBED`` translates with request URLs scrubbed, ``TRANSLATE``
    translates plainly. A handler-backed composite still relies on the head's
    operation sets (``None`` defers the scrub decision to the chat set) until
    its row lands.
    """
    if isinstance(row, CustomBinding):
        return (
            row.error_mode is ErrorMode.RAW_PASSTHROUGH,
            row.error_mode is ErrorMode.TRANSLATE_SCRUBBED,
        )
    return operation in _RAW_PASSTHROUGH_HANDLER_OPERATIONS, None


class WebRpcBackend(ChatWebHandlers):
    """Typed semantic binding owning web execution through its runtime."""

    def __init__(
        self,
        runtime: WebExecutionRuntime,
        *,
        transport_factory: Callable[..., object],
        source_uploader: Any | None = None,
        chat_transport: RuntimeTransport | None = None,
        chat_reqid: ReqidCounter | None = None,
        chat_timeout: float | None = None,
        chat_response_max_bytes: int | None = None,
        deadline_factory: RuntimeDeadlineFactory | None = None,
        metrics: ClientMetrics | None = None,
        drain_tracker: TransportDrainTracker | None = None,
        reqid: ReqidCounter | None = None,
        pipeline: RuntimePipeline | None = None,
        provider: WebCookieProvider | None = None,
        session: WebCookieSession | None = None,
        owns_provider: bool = False,
    ) -> None:
        assert_resolved_read_timeout(chat_timeout, name="chat_timeout")
        # One backend-owned runtime serves the public raw-RPC escape hatch and
        # semantic dispatch, so execution authority does not reside in the
        # general client adapter.
        self._runtime = runtime
        self._provider = provider
        self._owns_provider = owns_provider
        self._provider_closed = False
        self._provider_close_task: asyncio.Task[None] | None = None
        self._backend_session = session
        self._metrics = metrics
        self._drain_tracker = drain_tracker
        self._reqid = reqid
        self._pipeline = pipeline
        self._transport_factory = transport_factory
        self._source_uploader = source_uploader
        self._chat_transport = chat_transport
        self._chat_reqid = chat_reqid
        self._chat_timeout = chat_timeout
        self._chat_response_max_bytes = chat_response_max_bytes
        self._deadline_factory = deadline_factory
        self._capabilities = BackendCapabilities(
            supported_operations=WEB_SUPPORTED_OPERATIONS,
        )
        self._closed = False
        # The transport reads the runtime through the shell on every call so
        # a post-construction rebinding of ``_runtime`` is observed by dispatch.
        self._transport = WebTransport(
            runtime_provider=lambda: self._runtime,
            chat_transport=chat_transport,
            chat_response_max_bytes=chat_response_max_bytes,
        )
        # Resolve every registry handler name exactly once. A misnamed or
        # missing handler fails here, at construction, rather than on that
        # operation's first invocation.
        self._bindings = _resolve_handler_bindings(self)
        _configure_default_upload_backend(self)

    @property
    def kind(self) -> BackendKind:
        return BackendKind.WEB

    @property
    def provider(self) -> WebCookieProvider:
        """Return the injected credential-provider port."""
        if self._provider is None:
            raise RuntimeError("WebRpcBackend has no provider")
        return self._provider

    @property
    def _kernel(self) -> Any | None:
        """Compatibility inspection view over the private backend session."""
        session = self._backend_session
        return getattr(session, "kernel", None) if session is not None else None

    @property
    def runtime_ready(self) -> bool:
        """Whether atomic client-runtime assembly completed before publication."""
        return (
            self._backend_session is not None
            and self._provider is not None
            and self._pipeline is not None
        )

    @property
    def retry_limits(self) -> tuple[int, int]:
        """Return the immutable retry budgets owned by the runtime pipeline."""
        if self._pipeline is None:
            raise RuntimeError("WebRpcBackend has no retry configuration")
        return (
            self._pipeline.rate_limit_max_retries,
            self._pipeline.server_error_max_retries,
        )

    async def open_client(
        self,
        *,
        uploader: SourceUploadPipeline,
        chat: ChatAPI,
    ) -> None:
        """Open provider acquisition, then seed the private backend session."""
        session = self._backend_session
        if session is None or self._provider is None:
            raise RuntimeError("WebRpcBackend has no complete client lifecycle")
        try:
            await self.provider.open(uploader=uploader, chat=chat)
            await session.open(await self.provider.generation())
        except BaseException:
            try:
                await asyncio.shield(session.close())
            finally:
                if self._owns_provider:
                    await asyncio.shield(self.provider.close())
            raise
        self._provider_closed = False
        self._provider_close_task = None

    async def drain_client(self, timeout: float | None = None) -> None:
        """Stop admission and wait for client-owned work to settle."""
        if self._drain_tracker is None:
            raise RuntimeError("WebRpcBackend has no client drain owner")
        await self._drain_tracker.drain(timeout=timeout)

    async def close_client(
        self,
        *,
        drain: bool = True,
        drain_timeout: float | None = None,
    ) -> None:
        """Close the client lifecycle while preserving drain/cancel arbitration."""
        drain_tracker = self._drain_tracker
        session = self._backend_session
        if drain_tracker is None or session is None or self._provider is None:
            raise RuntimeError("WebRpcBackend has no complete client lifecycle")

        async def close_lifecycle(*, reconcile_backend: bool = True) -> None:
            try:
                if self._owns_provider:
                    await self._close_owned_provider(reconcile_backend=reconcile_backend)
                elif reconcile_backend:
                    await self.provider.reconcile()
            finally:
                await session.close()

        if not drain:
            await close_lifecycle(reconcile_backend=False)
            return

        drain_timeout_exc: TimeoutError | None = None
        try:
            if not (drain_timeout is not None and drain_timeout < 0):
                await drain_tracker.begin_drain()
            await drain_tracker.run_drain_hooks()
            await drain_tracker.drain(timeout=drain_timeout)
        except TimeoutError as exc:
            drain_timeout_exc = exc
        except asyncio.CancelledError:
            try:
                await asyncio.shield(close_lifecycle(reconcile_backend=False))
            except (Exception, asyncio.CancelledError):
                pass
            raise

        try:
            await asyncio.shield(close_lifecycle(reconcile_backend=drain_timeout_exc is None))
        except Exception as close_exc:
            if drain_timeout_exc is not None:
                logging.getLogger(__name__).warning(
                    "Suppressing close() error after drain timeout to preserve timeout signal: %s",
                    close_exc,
                )
                raise drain_timeout_exc from close_exc
            raise
        if drain_timeout_exc is not None:
            raise drain_timeout_exc

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        if self._metrics is None:
            raise RuntimeError("WebRpcBackend has no client metrics owner")
        return self._metrics.snapshot()

    async def public_rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        allow_null: bool = False,
        *,
        disable_internal_retries: bool = False,
        read_timeout: float | None = None,
        raise_on_null_status: bool = False,
    ) -> Any:
        return await self._runtime.rpc_call(
            method=method,
            params=params,
            allow_null=allow_null,
            disable_internal_retries=disable_internal_retries,
            read_timeout=read_timeout,
            raise_on_null_status=raise_on_null_status,
        )

    @property
    def is_connected(self) -> bool:
        return self._backend_session is not None and self._backend_session.is_open

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _capture_public_failure(
        self,
        exc: Exception,
        *,
        operation: Operation,
    ) -> SourceAddFailureRecord:
        """Project the bounded public error graph for source workflow receipts."""
        return _capture_public_failure(exc, operation=operation)

    def _row_collaborators(self) -> Mapping[str, object]:
        """The closed, named collaborator set a custom row may declare and reach.

        Built per invocation so the head keeps no extra instance state (the P8
        ``vars(backend)`` regressions pin the attribute set); the names are
        audited against every custom row's declaration at construction.
        """
        return MappingProxyType(
            {
                "source_uploader": self._source_uploader,
                "deadline_factory": self._deadline_factory,
                "capture_public_failure": self._capture_public_failure,
            }
        )

    async def invoke(
        self,
        operation: OperationDef[Any, Any],
        value: Any,
        *,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        """Validate and dispatch one typed operation through its web binding."""
        binding = WEB_OPERATION_REGISTRY.get(operation.key)
        if binding is None or not binding.is_supported:
            raise UnsupportedOperationError(operation.key, self.kind)
        if binding.definition != operation:
            raise BackendContractError(
                f"non-canonical definition supplied for {operation.key.value}"
            )
        if type(value) is not operation.input_type:
            raise BackendContractError(
                f"{operation.key.value} requires {operation.input_type.__name__}, "
                f"got {type(value).__name__}"
            )
        if deadline is not None and not isinstance(deadline, RuntimeDeadline):
            raise BackendContractError("deadline must be RuntimeDeadline or None")
        if (
            deadline is None
            and self._deadline_factory is not None
            and operation.key in CLIENT_TIMEOUT_DEADLINE_OPERATIONS
        ):
            # Capture once at the service/backend handoff. Every native phase
            # below receives this exact absolute identity; upload, polling,
            # chat, and research workflows remain on their reviewed budgets.
            deadline = self._deadline_factory.start()
        if self._closed:
            raise BackendContractError("WebRpcBackend is closed")
        if deadline is not None and deadline.expired():
            raise BackendDeadlineExceededError(
                operation.key,
                diagnostics=MappingProxyType(
                    {
                        "timeout": deadline.timeout,
                        "remaining": deadline.remaining(),
                        "timeout_seconds": deadline.timeout,
                    }
                ),
            )

        raw_passthrough, scrub_request_urls = _row_error_projection(
            self._bindings.get(operation.key), operation.key
        )
        try:
            result = await invoke_binding(
                self._bindings,
                self._transport,
                self._translate_native_error,
                operation,
                value,
                deadline=deadline,
                collaborators=self._row_collaborators(),
            )
        except BackendError:
            raise
        except RPCTimeoutError as exc:
            if raw_passthrough:
                raise
            if deadline is not None and deadline.expired():
                diagnostics = dict(self._error_diagnostics(exc, BackendErrorReason.TIMEOUT))
                diagnostics.update({"timeout": deadline.timeout, "remaining": deadline.remaining()})
                diagnostics["public_error_failure"] = _capture_public_failure(
                    exc,
                    operation=operation.key,
                )
                raise BackendDeadlineExceededError(
                    operation.key,
                    outcome_unknown=operation.policy is not CallPolicy.READ,
                    diagnostics=MappingProxyType(diagnostics),
                    dispatched=bool(getattr(exc, "dispatched", False)),
                ) from exc
            translated = translate_web_error(
                operation.key, exc, scrub_request_urls=scrub_request_urls
            )
            raise translated from exc
        except NotebookLMError as exc:
            # Source registration/upload compatibility requires the original
            # exception object (not merely its class/payload), especially after
            # file registration where callers inspect source_id/stage and the
            # original causal chain. These workflows are backend-owned, but
            # deliberately re-raise their established public leaves unchanged.
            if raw_passthrough:
                raise
            # Catch the closed library family rather than a broad ``RPCError``
            # wrap.  ``_translate_error`` still accepts only the exact reviewed
            # transport types and fails closed for any semantic exception.
            if not isinstance(exc, (RPCError, NetworkError, IdempotencyVariantError, ChatError)):
                raise BackendContractError(
                    f"unclassified web error type {type(exc).__module__}.{type(exc).__qualname__}",
                    operation=operation.key,
                ) from exc
            translated = translate_web_error(
                operation.key, exc, scrub_request_urls=scrub_request_urls
            )
            raise translated from exc

        if type(result) is not operation.output_type:
            raise BackendContractError(
                f"{operation.key.value} returned {type(result).__name__}, "
                f"expected {operation.output_type.__name__}"
            )
        return result

    async def close(self) -> None:
        """Close dispatch and any provider created by a convenience factory."""
        self._closed = True
        try:
            if self._owns_provider:
                await self._close_owned_provider()
            elif self._provider is not None and self._backend_session is not None:
                await self._provider.reconcile()
        finally:
            if self._backend_session is not None:
                await self._backend_session.close()

    async def _close_owned_provider(self, *, reconcile_backend: bool = True) -> None:
        provider = self._provider
        if not self._owns_provider or provider is None or self._provider_closed:
            return
        task = self._provider_close_task
        if task is None or (task.done() and not task.cancelled() and task.exception() is not None):
            close = (
                provider.close() if reconcile_backend else provider.close(reconcile_backend=False)
            )
            task = asyncio.create_task(close)
            self._provider_close_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            if task.done():
                self._provider_close_task = None
            raise
        else:
            self._provider_closed = True

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        raise_on_null_status: bool = False,
        outcome_unknown_on_expiry: bool = False,
        attempt_timeout: float | None = None,
    ) -> Any:
        """Delegate one native call to the transport under the semantic deadline.

        ``_is_retry`` is accepted only for signature compatibility: it is the
        runtime's own auth-refresh recursion flag and never a handler input.
        """
        del _is_retry
        return await self._transport.call(
            WebRequest(
                operation=operation,
                method=method,
                params=params,
                source_path=source_path,
                operation_variant=operation_variant,
                allow_null=allow_null,
                raise_on_null_status=raise_on_null_status,
                disable_internal_retries=disable_internal_retries,
                outcome_unknown_on_expiry=outcome_unknown_on_expiry,
                attempt_timeout=attempt_timeout,
            ),
            deadline=deadline,
        )

    async def _list_notebooks(
        self,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> NotebookListResult:
        """List notebooks for a composite (create baseline/probe, quota verification).

        The semantic ``notebook.list`` operation is the ``NOTEBOOK_LIST`` codec
        row; this helper issues the same native call under the composite's own
        operation attribution and deadline.
        """
        result = await self._rpc_call(
            RPCMethod.LIST_NOTEBOOKS,
            encode_list_notebooks(),
            operation=operation,
            deadline=deadline,
        )
        return decode_notebook_list_result(result)

    async def _notebook_create(
        self,
        value: NotebookCreateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> NotebookCreateResult:
        baseline_ids: set[str] | None
        baseline_error: Exception | None = None
        try:
            baseline = await self._list_notebooks(
                operation=Operation.NOTEBOOK_CREATE,
                deadline=deadline,
            )
            baseline_ids = {notebook.id for notebook in baseline.notebooks}
        except Exception as exc:
            baseline_ids = None
            baseline_error = exc
            notebook_logger.warning(
                "create: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a notebook this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                type(exc).__name__,
                exc_info=True,
            )

        async def create() -> NotebookRecord:
            try:
                result = await self._rpc_call(
                    RPCMethod.CREATE_NOTEBOOK,
                    build_create_notebook_params(value.title),
                    operation=Operation.NOTEBOOK_CREATE,
                    deadline=deadline,
                    disable_internal_retries=True,
                )
            except RPCError as exc:
                limit_error = await self._notebook_limit_error(exc, deadline=deadline)
                if limit_error is not None:
                    raise limit_error from None
                raise
            return decode_notebook(result)

        async def probe() -> NotebookRecord | None:
            try:
                current = await self._list_notebooks(
                    operation=Operation.NOTEBOOK_CREATE,
                    deadline=deadline,
                )
            except RPCTimeoutError:
                # The outer semantic dispatch owns timeout translation. Let it
                # retain notebook.create attribution and mark the post-write
                # reconciliation outcome unknown.
                raise
            except (AuthError, RateLimitError, ServerError, NetworkError) as exc:
                notebook_logger.warning(
                    "create: probe list() failed with transport/auth error; "
                    "propagating so the caller can avoid a duplicate-resource retry"
                )
                mark_unconfirmed(exc)
                raise
            except BackendError as exc:
                raise mark_backend_outcome_unknown(exc) from exc
            except Exception as exc:
                notebook_logger.warning(
                    "create: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    type(exc).__name__,
                    exc_info=True,
                )
                raise mark_unconfirmed(
                    RPCError(
                        "UNRESOLVED — do not blindly retry; check your notebook list "
                        f"first. Cannot confirm notebook with title {value.title!r}: the "
                        "create failed at the transport level and may or may not have "
                        "committed, and the idempotency probe that would settle it "
                        f"failed too ({type(exc).__name__}). No FURTHER attempt was made.",
                        method_id=RPCMethod.CREATE_NOTEBOOK.value,
                    )
                ) from exc
            matches = tuple(
                notebook for notebook in current.notebooks if notebook.title == value.title
            )
            if baseline_ids is not None:
                matches = tuple(notebook for notebook in matches if notebook.id not in baseline_ids)
            elif matches:
                raise mark_unconfirmed(
                    RPCError(
                        f"Cannot disambiguate notebook with title {value.title!r} — check your "
                        "notebook list before retrying: the pre-create baseline snapshot failed "
                        f"({type(baseline_error).__name__}), so "
                        f"{', '.join(f'{item.id} ({item.title!r})' for item in matches)} may "
                        "either predate this create or be the notebook it just created.",
                        method_id=RPCMethod.CREATE_NOTEBOOK.value,
                    )
                )
            if len(matches) == 1:
                return next(iter(matches))
            if len(matches) > 1:
                raise mark_unconfirmed(
                    RPCError(
                        f"Cannot disambiguate notebook with title {value.title!r}: "
                        f"probe found {len(matches)} new notebooks with this title",
                        method_id=RPCMethod.CREATE_NOTEBOOK.value,
                    )
                )
            return None

        result = await idempotent_create(
            create,
            probe,
            may_have_committed=transport_may_have_committed,
            label=f"notebook.create[{value.title!r}]",
        )
        return NotebookCreateResult(notebook=result.value)

    async def _notebook_limit_error(
        self,
        error: RPCError,
        *,
        deadline: RuntimeDeadline | None,
    ) -> BackendError | None:
        if (
            error.method_id != RPCMethod.CREATE_NOTEBOOK.value
            or error.rpc_code != _CREATE_NOTEBOOK_QUOTA_RPC_CODE
        ):
            return None
        try:
            settings = await self._rpc_call(
                RPCMethod.GET_USER_SETTINGS,
                settings_codec.encode_get_user_settings(),
                operation=Operation.NOTEBOOK_CREATE,
                deadline=deadline,
                source_path="/",
            )
            limit = settings_codec.decode_account_limits(settings).notebook_limit
        except Exception:
            notebook_logger.debug(
                "Could not fetch account limits after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return None
        if limit is None:
            return None
        try:
            listed = await self._list_notebooks(
                operation=Operation.NOTEBOOK_CREATE,
                deadline=deadline,
            )
        except Exception:
            notebook_logger.debug(
                "Could not list notebooks after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return None
        owned_count = sum(1 for notebook in listed.notebooks if notebook.is_owner)
        if owned_count < max(limit - 1, 0):
            return None

        original = self._translate_error(Operation.NOTEBOOK_CREATE, error)
        return BackendError(
            message="notebook limit reached",
            operation=Operation.NOTEBOOK_CREATE,
            diagnostics=MappingProxyType(
                {
                    "current_count": owned_count,
                    "limit": limit,
                    "original_message": original.message,
                    "original_reason": original.reason.value
                    if original.reason is not None
                    else None,
                    "original_diagnostics": dict(original.diagnostics or {}),
                }
            ),
            reason=BackendErrorReason.NOTEBOOK_LIMIT,
        )

    async def _notebook_update(
        self,
        value: NotebookUpdateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> NotebookUpdateResult:
        await self._rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            build_update_notebook_params(
                value.notebook_id,
                title=value.title,
                emoji=value.emoji,
            ),
            operation=Operation.NOTEBOOK_UPDATE,
            deadline=deadline,
            source_path="/",
            allow_null=True,
        )
        try:
            result = await self._rpc_call(
                RPCMethod.GET_NOTEBOOK,
                build_get_notebook_params(value.notebook_id),
                operation=Operation.NOTEBOOK_UPDATE,
                deadline=deadline,
                source_path=f"/notebook/{value.notebook_id}",
                outcome_unknown_on_expiry=True,
            )
        except ClientError as exc:
            if normalize_grpc_status(exc.rpc_code) is not GrpcStatusCode.NOT_FOUND:
                raise
            diagnostics = dict(self._error_diagnostics(exc, BackendErrorReason.CLIENT))
            diagnostics.update(
                {
                    "notebook_id": value.notebook_id,
                    "method_id": RPCMethod.GET_NOTEBOOK.value,
                    "detail": str(exc),
                    "original_message": str(exc.args[0]) if exc.args else str(exc),
                }
            )
            raise BackendError(
                message=f"Notebook not found: {value.notebook_id}",
                operation=Operation.NOTEBOOK_UPDATE,
                diagnostics=MappingProxyType(diagnostics),
                reason=BackendErrorReason.NOTEBOOK_NOT_FOUND,
            ) from exc
        notebook_row = (
            safe_index(
                result,
                0,
                method_id=RPCMethod.GET_NOTEBOOK.value,
                source="WebRpcBackend._notebook_update",
            )
            if result and isinstance(result, list)
            else None
        )
        if not notebook_row:
            raise BackendError(
                message=f"Notebook not found: {value.notebook_id}",
                operation=Operation.NOTEBOOK_UPDATE,
                diagnostics=MappingProxyType(
                    {
                        "notebook_id": value.notebook_id,
                        "method_id": RPCMethod.GET_NOTEBOOK.value,
                    }
                ),
                reason=BackendErrorReason.NOTEBOOK_NOT_FOUND,
            )
        notebook = decode_notebook(notebook_row, include_chat_settings=True)
        if not notebook.id and not notebook.title:
            raise BackendError(
                message=f"Notebook not found: {value.notebook_id}",
                operation=Operation.NOTEBOOK_UPDATE,
                diagnostics=MappingProxyType(
                    {
                        "notebook_id": value.notebook_id,
                        "method_id": RPCMethod.GET_NOTEBOOK.value,
                    }
                ),
                reason=BackendErrorReason.NOTEBOOK_NOT_FOUND,
            )
        return NotebookUpdateResult(notebook=notebook)

    async def _artifact_catalog_records(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        include_mind_maps: bool,
        outcome_unknown_on_expiry: bool = False,
    ) -> tuple[ArtifactRecord, ...]:
        result = await self._rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            [
                [2],
                notebook_id,
                f'NOT artifact.status = "{ARTIFACT_STATUS_SUGGESTED_WIRE_NAME}"',
            ],
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        if isinstance(result, list):
            rows = unwrap_artifact_rows(
                result,
                method_id=RPCMethod.LIST_ARTIFACTS.value,
                source="WebRpcBackend._artifact_catalog_records",
            )
        elif not result:
            rows = []
        else:
            raise DecodingError(
                "Unrecognized LIST_ARTIFACTS payload shape",
                raw_response=reprlib.repr(result),
                method_id=RPCMethod.LIST_ARTIFACTS.value,
            )

        artifacts = [decode_artifact(row) for row in rows if isinstance(row, list) and row]
        if include_mind_maps:
            caller = DeadlineRpcCaller(self, deadline, operation)
            mind_maps = NoteBackedMindMapService(LegacyNoteBackedService(cast(Any, caller)))
            try:
                mind_map_rows = await mind_maps.list_mind_maps(notebook_id)
                artifacts.extend(
                    artifact
                    for row in mind_map_rows
                    if (artifact := decode_mind_map_artifact(row)) is not None
                )
            except DecodingError:
                raise
            except (RPCError, httpx.HTTPError) as exc:
                # Most transport failures are normalized before this composite,
                # but an auth-refresh failure deliberately re-raises its original
                # HTTPStatusError. Preserve the legacy partial-availability net
                # for that raw leaf as well as ordinary RPC failures.
                artifact_logger.warning("Failed to fetch mind maps: %s", exc)
        return tuple(artifacts)

    async def _artifact_list(
        self,
        value: ArtifactListInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ArtifactListResult:
        records = await self._artifact_catalog_records(
            value.notebook_id,
            operation=Operation.ARTIFACT_LIST,
            deadline=deadline,
            include_mind_maps=value.family in {None, "mind_map"},
        )
        return ArtifactListResult(artifacts=records)

    async def _artifact_get(
        self,
        value: ArtifactGetInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ArtifactGetResult:
        records = await self._artifact_catalog_records(
            value.notebook_id,
            operation=Operation.ARTIFACT_GET,
            deadline=deadline,
            include_mind_maps=True,
        )
        return ArtifactGetResult(
            artifact=next((item for item in records if item.id == value.artifact_id), None)
        )

    async def _persist_generated_mind_map(
        self,
        notebook_id: str,
        *,
        title: str,
        content: str,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> tuple[str | None, datetime | None]:
        caller = DeadlineRpcCaller(self, deadline, operation)
        note = await LegacyNoteBackedService(cast(Any, caller)).create_note(
            notebook_id,
            title=title,
            content=content,
        )
        return note.id or None, note.created_at

    async def _mind_map_generate_note(
        self,
        value: MindMapGenerateNoteInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> MindMapGenerateNoteResult:
        source_ids = value.source_ids
        if source_ids is None:
            notebook = await self._rpc_call(
                RPCMethod.GET_NOTEBOOK,
                build_get_notebook_params(value.notebook_id),
                operation=Operation.MIND_MAP_GENERATE_NOTE,
                deadline=deadline,
                source_path=f"/notebook/{value.notebook_id}",
            )
            source_ids = self._audio_source_ids(notebook)
        result = await self._rpc_call(
            RPCMethod.GENERATE_MIND_MAP,
            build_mind_map_params(
                list(source_ids),
                language=(get_default_language() if value.language is None else value.language),
                instructions=value.instructions,
            ),
            operation=Operation.MIND_MAP_GENERATE_NOTE,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
            operation_variant=None,
        )
        return MindMapGenerateNoteResult(decode_generated_tree(result))

    async def _mind_map_generate_interactive(
        self,
        value: MindMapGenerateInteractiveInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> MindMapGenerateInteractiveResult:
        source_ids = value.source_ids
        if source_ids is None:
            notebook = await self._rpc_call(
                RPCMethod.GET_NOTEBOOK,
                build_get_notebook_params(value.notebook_id),
                operation=Operation.MIND_MAP_GENERATE_INTERACTIVE,
                deadline=deadline,
                source_path=f"/notebook/{value.notebook_id}",
            )
            source_ids = self._audio_source_ids(notebook)
        result = await self._rpc_call(
            RPCMethod.CREATE_ARTIFACT,
            build_interactive_mind_map_artifact_params(
                value.notebook_id,
                list(source_ids),
                instructions=value.instructions,
            ),
            operation=Operation.MIND_MAP_GENERATE_INTERACTIVE,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
            operation_variant=None,
        )
        mind_map_id = decode_created_interactive_id(result)
        if mind_map_id is None:
            raise self._artifact_feature_unavailable(
                Operation.MIND_MAP_GENERATE_INTERACTIVE,
                "mind_map",
            )
        return MindMapGenerateInteractiveResult(mind_map_id)

    @staticmethod
    def _error_diagnostics(
        exc: RPCError | NetworkError | IdempotencyVariantError | ChatError,
        reason: BackendErrorReason,
    ) -> MappingProxyType[str, object]:
        return error_diagnostics(exc, reason)

    def _translate_native_error(self, operation: Operation, error: Exception) -> BackendError:
        """Typed :class:`ErrorTranslator` view over the reviewed transport types."""
        if not isinstance(error, (RPCError, NetworkError, IdempotencyVariantError, ChatError)):
            raise BackendContractError(
                f"unclassified web error type {type(error).__module__}.{type(error).__qualname__}",
                operation=operation,
            ) from error
        return self._translate_error(operation, error)

    @classmethod
    def _translate_error(
        cls,
        operation: Operation,
        exc: RPCError | NetworkError | IdempotencyVariantError | ChatError,
    ) -> BackendError:
        """Delegate to the shared ``_web.errors`` translation (kept on the head for callers)."""
        return translate_web_error(operation, exc)


def _configure_default_upload_backend(backend: WebRpcBackend) -> None:
    """Install the pipeline's default callbacks under the ``SOURCE_ADD_FILE`` row.

    P9.4b (plan open item 1): the defaults execute through the row's own invoker
    — its declared natives, options and failure tagging — so the legacy
    registration helper never bypasses the binding table; the row binds a fresh,
    invocation-scoped set on every ``source.add_file`` call.
    """
    uploader = backend._source_uploader
    if uploader is None:
        return
    default = upload_backend(
        row_invoker(
            backend._bindings,
            backend._transport,
            backend._translate_native_error,
            Operation.SOURCE_ADD_FILE,
        )
    )
    uploader.configure_source_limit_lookup(default.get_source_limit)
    uploader.configure_source_backend(
        list_sources=default.list_sources,
        register_file_source=default.register_file_source,
        rename_source=default.rename_source,
    )


def _resolve_handler_bindings(
    backend: WebRpcBackend,
    *,
    registry: Mapping[Operation, WebOperationBinding] = WEB_OPERATION_REGISTRY,
    supported: frozenset[Operation] = WEB_SUPPORTED_OPERATIONS,
) -> BindingTable:
    """Assemble the construction-audited binding table.

    Row-backed operations use their ``_web.bindings`` row as-is; handler-backed
    operations resolve their registry handler name exactly once here, so a
    misnamed or missing handler fails at construction.
    """
    rows: dict[Operation, Binding] = {}
    for operation, binding in registry.items():
        definition = binding.definition
        executable = (
            definition is not None and binding.disposition is OperationDisposition.SUPPORTED_DIRECT
        )
        if binding.row is not None:
            if executable:
                rows[operation] = binding.row
            continue
        handler_name = binding.handler_name
        if handler_name is None:
            continue
        handler = getattr(backend, handler_name, None)
        if handler is None or not callable(handler):
            raise BackendContractError(
                f"{operation.value} names missing web handler {handler_name!r}",
                operation=operation,
            )
        if definition is not None and executable:
            rows[operation] = ResolvedHandlerBinding(definition=definition, handler=handler)
    table = BindingTable(rows)
    try:
        audit_bindings(table, supported, collaborators=ROW_COLLABORATOR_NAMES)
    except BindingAuditError as exc:
        raise BackendContractError(f"web binding table rejected: {exc}") from exc
    return table


__all__ = ["WebRpcBackend"]
