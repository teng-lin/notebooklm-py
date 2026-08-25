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
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .._backend import (
    BackendCapabilities,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
)
from .._binding import (
    Binding,
    BindingAuditError,
    BindingTable,
    CodecBinding,
    CustomBinding,
    ErrorMode,
    OperationDisposition,
    audit_bindings,
    invoke_binding,
    row_invoker,
)
from .._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from .._operations import CallPolicy, Operation, OperationDef
from .._records import SourceAddFailureRecord
from .._runtime.config import assert_resolved_read_timeout
from .._web_cookie_provider import WebCookieProvider, WebCookieSession
from ..exceptions import (
    ChatError,
    IdempotencyVariantError,
    NetworkError,
    NotebookLMError,
    RPCError,
    RPCTimeoutError,
)
from ..rpc import RPCMethod
from ..types import ClientMetricsSnapshot
from .bindings.sources import upload_backend
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

source_logger = logging.getLogger("notebooklm").getChild("_sources")

#: The closed set of collaborator names the head supplies to custom rows. A row
#: declaring any other name is rejected by the construction-time audit.
ROW_COLLABORATOR_NAMES: frozenset[str] = frozenset(
    {
        "source_uploader",
        "deadline_factory",
        "capture_public_failure",
        # ``chat.ask`` (P9.4b): the request-id counter, the configured chat read
        # timeout, and whether the composed chat transport exists — never the
        # transport itself.
        "chat_reqid",
        "chat_timeout",
        "chat_transport_composed",
    }
)


def _row_error_projection(row: Binding | None, operation: Operation) -> tuple[bool, bool | None]:
    """Return ``(raw_passthrough, scrub_request_urls)`` for one operation's failures.

    A custom row carries its own projection as ``error_mode`` metadata:
    ``RAW_PASSTHROUGH`` re-raises the native exception unchanged,
    ``TRANSLATE_SCRUBBED`` translates with request URLs scrubbed, ``TRANSLATE``
    translates plainly. ``operation`` remains in this private helper's shape
    for the characterization tests that compare every row's projection.
    """
    del operation
    if isinstance(row, CustomBinding):
        return (
            row.error_mode is ErrorMode.RAW_PASSTHROUGH,
            row.error_mode is ErrorMode.TRANSLATE_SCRUBBED,
        )
    return False, None


class WebRpcBackend:
    """Typed semantic binding owning web execution through its runtime."""

    def __init__(
        self,
        runtime: WebExecutionRuntime,
        *,
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
        # P9.4c deleted the dead ``_transport_factory`` instance state and P10
        # R1.2 the constructor input that fed it; direct-HTTP owners receive
        # their factories from their own composition paths.
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
        self._bindings = _build_binding_table()
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
        """The closed, named collaborator set a custom row may declare and reach."""
        return _row_collaborators_of(self)

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
        # A codec row lets ``WebTransport.call`` raise the pre-dispatch expiry so
        # the error names the blocked native (``method_id``) exactly as the
        # composite handlers' ``_rpc_call`` did; nothing is dispatched either way.
        if (
            deadline is not None
            and deadline.expired()
            and not isinstance(binding.row, CodecBinding)
        ):
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


def _row_collaborators_of(backend: WebRpcBackend) -> Mapping[str, object]:
    """Build ``ROW_COLLABORATOR_NAMES`` → object for one invocation.

    Built per invocation so the head keeps no extra instance state (the P8
    ``vars(backend)`` regressions pin the attribute set); the names are audited
    against every custom row's declaration at construction, and the transport
    and runtime are never among them.
    """
    return MappingProxyType(
        {
            "source_uploader": backend._source_uploader,
            "deadline_factory": backend._deadline_factory,
            "capture_public_failure": backend._capture_public_failure,
            "chat_reqid": backend._chat_reqid,
            "chat_timeout": backend._chat_timeout,
            "chat_transport_composed": backend._chat_transport is not None,
        }
    )


def _build_binding_table(
    *,
    registry: Mapping[Operation, WebOperationBinding] = WEB_OPERATION_REGISTRY,
    supported: frozenset[Operation] = WEB_SUPPORTED_OPERATIONS,
) -> BindingTable:
    """Assemble and construction-audit the closed row-only binding table."""
    rows: dict[Operation, Binding] = {}
    for operation, binding in registry.items():
        if binding.disposition is not OperationDisposition.SUPPORTED_DIRECT:
            continue
        row = binding.row
        if row is not None:
            rows[operation] = row
    table = BindingTable(rows)
    try:
        audit_bindings(table, supported, collaborators=ROW_COLLABORATOR_NAMES)
    except BindingAuditError as exc:
        raise BackendContractError(f"web binding table rejected: {exc}") from exc
    return table


__all__ = ["WebRpcBackend"]
