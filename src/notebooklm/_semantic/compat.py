"""Project neutral backend failures onto the legacy public exception contract."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, TypeVar, cast

import httpx

from .._transport_errors import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
)
from ..exceptions import (
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
    AuthError,
    ChatError,
    ChatResponseParseError,
    ClientError,
    CollectionError,
    CollectionNotFoundError,
    DecodingError,
    IdempotencyVariantError,
    LabelError,
    LabelNotFoundError,
    NetworkError,
    NonIdempotentRetryError,
    NotebookLimitError,
    NotebookNotFoundError,
    NotFoundError,
    RateLimitError,
    ResearchStartUnavailableError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    ValidationError,
)
from ..rpc import RPCMethod
from .backend import BackendContractError, BackendError, BackendErrorReason
from .operations import Operation
from .records import LabelKind, SourceAddFailureKind, SourceAddFailureRecord

_T = TypeVar("_T")


# The legacy not-found ``method_id`` per workflow phase: a preflight or a
# membership readback reports the mutation the miss blocked, a field readback
# reports the read that proved absence (exactly what the P6.4 handler set).
_LABEL_NOT_FOUND_PHASE_METHOD_IDS: Mapping[str | None, str] = {
    "preflight": RPCMethod.UPDATE_LABEL.value,
    "membership_readback": RPCMethod.UPDATE_LABEL.value,
    "field_readback": RPCMethod.LIST_LABELS.value,
}
_ARTIFACT_NOT_FOUND_PHASE_METHOD_IDS: Mapping[str | None, str] = {
    "rename_readback": RPCMethod.RENAME_ARTIFACT.value,
}

# The service-owned source.update workflow reports only semantic absence. Its
# legacy method diagnostic belongs at this compatibility boundary, beside the
# equivalent label phase mapping, rather than importing web/RPC vocabulary into
# the transport-neutral Source service.
_SOURCE_NOT_FOUND_OPERATION_METHOD_IDS: Mapping[Operation, str] = {
    Operation.SOURCE_UPDATE: RPCMethod.UPDATE_SOURCE.value,
}


def _preserve_outcome(error: BackendError, projected: Exception) -> Exception:
    diagnostics = error.diagnostics or {}
    public_failure = diagnostics.get("public_error_failure")
    if public_failure is not None:
        if not isinstance(public_failure, SourceAddFailureRecord):
            raise BackendContractError(
                "backend compatibility public-error evidence has invalid type",
                operation=error.operation,
            )
        reason = error.reason
        expected_kind = (
            {
                BackendErrorReason.AUTH: SourceAddFailureKind.AUTH,
                BackendErrorReason.CHAT: SourceAddFailureKind.CHAT,
                BackendErrorReason.CHAT_RESPONSE_PARSE: SourceAddFailureKind.CHAT_RESPONSE_PARSE,
                BackendErrorReason.CLIENT: SourceAddFailureKind.CLIENT,
                BackendErrorReason.DECODING: SourceAddFailureKind.DECODING,
                BackendErrorReason.NETWORK: SourceAddFailureKind.NETWORK,
                BackendErrorReason.RATE_LIMIT: SourceAddFailureKind.RATE_LIMIT,
                BackendErrorReason.RESPONSE_TOO_LARGE: SourceAddFailureKind.RESPONSE_TOO_LARGE,
                BackendErrorReason.RPC: SourceAddFailureKind.RPC,
                BackendErrorReason.SERVER: SourceAddFailureKind.SERVER,
                BackendErrorReason.TIMEOUT: SourceAddFailureKind.RPC_TIMEOUT,
                BackendErrorReason.UNKNOWN_RPC_METHOD: SourceAddFailureKind.UNKNOWN_RPC_METHOD,
            }.get(reason)
            if reason is not None
            else None
        )
        if public_failure.kind is not expected_kind:
            raise BackendContractError(
                "backend compatibility public-error evidence disagrees with its reason",
                operation=error.operation,
            )
        replayed = _project_source_add_record(public_failure)
        if isinstance(projected, (NetworkError, RPCTimeoutError)) and isinstance(
            replayed, (NetworkError, RPCTimeoutError)
        ):
            projected.original_error = replayed.original_error
        projected.__cause__ = replayed.__cause__
        projected.__context__ = replayed.__context__
        projected.__suppress_context__ = replayed.__suppress_context__
    create_context_record = diagnostics.get("create_context_failure")
    if create_context_record is not None and not isinstance(
        create_context_record, SourceAddFailureRecord
    ):
        raise BackendContractError(
            "notebook-create context evidence has invalid type",
            operation=error.operation,
        )
    create_context = (
        _project_source_add_record(create_context_record)
        if isinstance(create_context_record, SourceAddFailureRecord)
        else None
    )
    probe_record = diagnostics.get("reconciliation_probe_failure")
    if probe_record is not None and not isinstance(probe_record, SourceAddFailureRecord):
        raise BackendContractError(
            "notebook-create probe evidence has invalid type",
            operation=error.operation,
        )
    if isinstance(probe_record, SourceAddFailureRecord):
        probe = _project_source_add_record(probe_record)
        if create_context is not None:
            probe.__context__ = create_context
        projected.__cause__ = probe
        projected.__context__ = probe
        projected.__suppress_context__ = True
    elif create_context is not None:
        projected.__context__ = create_context
    if error.outcome_unknown:
        projected.unconfirmed = True  # type: ignore[attr-defined]
    return projected


def _diagnostics(error: BackendError) -> Mapping[str, object]:
    diagnostics = error.diagnostics
    if diagnostics is None:
        raise BackendContractError(
            "backend compatibility error lacks diagnostics",
            operation=error.operation,
        )
    return diagnostics


def _optional(
    error: BackendError,
    diagnostics: Mapping[str, object],
    name: str,
    expected: type[object] | tuple[type[object], ...],
) -> object | None:
    value = diagnostics.get(name)
    if value is not None and not isinstance(value, expected):
        raise BackendContractError(
            f"backend compatibility diagnostic {name!r} has invalid type {type(value).__name__}",
            operation=error.operation,
        )
    return value


def _required_int(
    error: BackendError,
    diagnostics: Mapping[str, object],
    name: str,
) -> int | None:
    value = _optional(error, diagnostics, name, int)
    if isinstance(value, bool):
        raise BackendContractError(
            f"backend compatibility diagnostic {name!r} must not be bool",
            operation=error.operation,
        )
    return cast(int | None, value)


def _rpc_diagnostics(error: BackendError) -> dict[str, Any]:
    diagnostics = _diagnostics(error)
    method_id = _optional(error, diagnostics, "method_id", str)
    reconciliation = _optional(
        error,
        diagnostics,
        "notebook_create_reconciliation_unresolved",
        bool,
    )
    if method_id is None and reconciliation is True:
        method_id = RPCMethod.CREATE_NOTEBOOK.value
    raw_response = _optional(error, diagnostics, "raw_response", str)
    rpc_code = _optional(error, diagnostics, "rpc_code", (str, int))
    found_ids = diagnostics.get("found_ids")
    if found_ids is None:
        normalized_found_ids: list[str] = []
    elif isinstance(found_ids, list) and all(isinstance(item, str) for item in found_ids):
        normalized_found_ids = found_ids
    else:
        raise BackendContractError(
            "backend compatibility diagnostic 'found_ids' must be list[str] or None",
            operation=error.operation,
        )
    return {
        "method_id": method_id,
        "raw_response": raw_response,
        "rpc_code": rpc_code,
        "found_ids": normalized_found_ids,
    }


def _label_kind(error: BackendError, diagnostics: Mapping[str, object]) -> LabelKind:
    """Read the closed label/collection discriminator from backend diagnostics."""
    raw = _optional(error, diagnostics, "label_kind", str)
    try:
        return LabelKind(cast(str, raw))
    except ValueError as exc:
        raise BackendContractError(
            f"invalid label kind discriminator {raw!r}",
            operation=error.operation,
        ) from exc


def _project_original_rpc_error(
    error: BackendError,
    diagnostics: Mapping[str, object],
    *,
    label: str,
) -> RPCError:
    """Rebuild the RPC failure a diagnosed domain error was raised from.

    A backend that diagnoses a raw RPC rejection into a domain error (the
    notebook quota and the empty deep-research start) carries the rejecting
    call's own closed evidence alongside the diagnosis. Replaying it here is
    what lets the projected public error keep its ``__cause__`` without the
    backend ever handing an arbitrary exception object across the boundary.
    """
    original_reason = _optional(error, diagnostics, "original_reason", str)
    original_message = _optional(error, diagnostics, "original_message", str)
    original_diagnostics = diagnostics.get("original_diagnostics")
    if (
        original_reason is None
        or original_message is None
        or not isinstance(original_diagnostics, Mapping)
    ):
        raise BackendContractError(
            f"{label} compatibility error lacks original RPC evidence",
            operation=error.operation,
        )
    try:
        nested_reason = BackendErrorReason(original_reason)
    except ValueError as exc:
        raise BackendContractError(
            f"invalid {label} original reason {original_reason!r}",
            operation=error.operation,
        ) from exc
    original = project_backend_error(
        BackendError(
            cast(str, original_message),
            operation=error.operation,
            diagnostics=original_diagnostics,
            reason=nested_reason,
        )
    )
    if not isinstance(original, RPCError):
        raise BackendContractError(
            f"{label} original evidence does not reconstruct RPCError",
            operation=error.operation,
        )
    return original


def project_backend_error(error: BackendError) -> Exception:
    """Reconstruct the exact public exception class from closed neutral evidence."""
    reason = error.reason
    if reason is None:
        raise BackendContractError(
            "backend compatibility error lacks a closed reason",
            operation=error.operation,
        )
    diagnostics = _diagnostics(error)

    if reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE:
        artifact_type = _optional(error, diagnostics, "artifact_type", str)
        if artifact_type is None:
            raise BackendContractError(
                "artifact-feature-unavailable compatibility error lacks artifact_type",
                operation=error.operation,
            )
        return _preserve_outcome(
            error,
            ArtifactFeatureUnavailableError(
                cast(str, artifact_type),
                method_id=cast(str | None, _optional(error, diagnostics, "method_id", str)),
                raw_response=cast(str | None, _optional(error, diagnostics, "raw_response", str)),
            ),
        )

    if reason is BackendErrorReason.ARTIFACT_NOT_FOUND:
        artifact_id = _optional(error, diagnostics, "artifact_id", str)
        if artifact_id is None:
            raise BackendContractError(
                "artifact-not-found compatibility error lacks artifact_id",
                operation=error.operation,
            )
        artifact_method_id = cast(str | None, _optional(error, diagnostics, "method_id", str))
        if artifact_method_id is None:
            # The service-owned rename workflow records the neutral phase; the
            # compatibility projector owns the legacy wire-method diagnostic.
            artifact_method_id = _ARTIFACT_NOT_FOUND_PHASE_METHOD_IDS.get(
                cast(str | None, _optional(error, diagnostics, "phase", str))
            )
        return _preserve_outcome(
            error,
            ArtifactNotFoundError(
                cast(str, artifact_id),
                cast(str | None, _optional(error, diagnostics, "artifact_type", str)),
                method_id=artifact_method_id,
                raw_response=cast(str | None, _optional(error, diagnostics, "raw_response", str)),
            ),
        )

    if reason is BackendErrorReason.SOURCE_ADD:
        record = diagnostics.get("source_add_failure")
        if not isinstance(record, SourceAddFailureRecord):
            raise BackendContractError(
                "source.add_url backend failure lacks SourceAddFailureRecord",
                operation=error.operation,
            )
        return _preserve_outcome(error, _project_source_add_record(record))

    if reason is BackendErrorReason.IDEMPOTENCY_VARIANT:
        return _preserve_outcome(error, IdempotencyVariantError(error.message))

    if reason is BackendErrorReason.CHAT:
        return _preserve_outcome(error, ChatError(error.message))
    if reason is BackendErrorReason.CHAT_RESPONSE_PARSE:
        return _preserve_outcome(error, ChatResponseParseError(error.message))

    if reason is BackendErrorReason.NETWORK:
        return _preserve_outcome(
            error,
            NetworkError(
                error.message,
                method_id=cast(str | None, _optional(error, diagnostics, "method_id", str)),
            ),
        )
    if reason is BackendErrorReason.TIMEOUT:
        return _preserve_outcome(
            error,
            RPCTimeoutError(
                error.message,
                method_id=cast(str | None, _optional(error, diagnostics, "method_id", str)),
                timeout_seconds=cast(
                    float | None,
                    _optional(error, diagnostics, "timeout_seconds", (float, int)),
                ),
            ),
        )
    if reason is BackendErrorReason.UNKNOWN_RPC_METHOD:
        method_id = _optional(error, diagnostics, "method_id", (str, int))
        rpc_code = _optional(error, diagnostics, "rpc_code", (str, int))
        found_ids = diagnostics.get("found_ids")
        if found_ids is not None and not (
            isinstance(found_ids, list) and all(isinstance(item, (str, int)) for item in found_ids)
        ):
            raise BackendContractError(
                "unknown-RPC found_ids must be list[str | int] or None",
                operation=error.operation,
            )
        path = diagnostics.get("path")
        if path is not None and not (
            isinstance(path, tuple)
            and all(isinstance(item, int) and not isinstance(item, bool) for item in path)
        ):
            raise BackendContractError(
                "unknown-RPC path must be tuple[int, ...] or None",
                operation=error.operation,
            )
        source = _optional(error, diagnostics, "source", str)
        return _preserve_outcome(
            error,
            UnknownRPCMethodError(
                error.message,
                method_id=cast(str | int | None, method_id),
                path=cast(tuple[int, ...] | None, path),
                source=cast(str | None, source),
                found_ids=cast(list[str | int] | None, found_ids),
                raw_response=diagnostics.get("raw_response"),
                data_at_failure=diagnostics.get("data_at_failure"),
                rpc_code=cast(str | int | None, rpc_code),
            ),
        )
    if reason is BackendErrorReason.NOTEBOOK_NOT_FOUND:
        notebook_id = _optional(error, diagnostics, "notebook_id", str)
        if notebook_id is None:
            raise BackendContractError(
                "notebook-not-found compatibility error lacks notebook_id",
                operation=error.operation,
            )
        method_id = cast(str | None, _optional(error, diagnostics, "method_id", str))
        if method_id is None:
            method_id = RPCMethod.GET_NOTEBOOK.value
        raw_response = cast(str | None, _optional(error, diagnostics, "raw_response", str))
        rpc_code = cast(
            str | int | None,
            _optional(error, diagnostics, "rpc_code", (str, int)),
        )
        found_ids = diagnostics.get("found_ids")
        if found_ids is None:
            normalized_found_ids: list[str] = []
        elif isinstance(found_ids, list) and all(isinstance(item, str) for item in found_ids):
            normalized_found_ids = found_ids
        else:
            raise BackendContractError(
                "notebook-not-found found_ids must be list[str] or None",
                operation=error.operation,
            )
        detail = cast(str | None, _optional(error, diagnostics, "detail", str))
        not_found_projected = NotebookNotFoundError(
            cast(str, notebook_id),
            method_id=method_id,
            raw_response=raw_response,
            rpc_code=rpc_code,
            found_ids=normalized_found_ids,
            detail=detail,
        )
        not_found_original_message = cast(
            str | None,
            _optional(error, diagnostics, "original_message", str),
        )
        if not_found_original_message is not None:
            not_found_original = ClientError(
                not_found_original_message,
                status_code=_required_int(error, diagnostics, "status_code"),
                method_id=method_id,
                raw_response=raw_response,
                rpc_code=rpc_code,
                found_ids=normalized_found_ids,
            )
            not_found_projected.__cause__ = not_found_original
            not_found_projected.__context__ = not_found_original
            not_found_projected.__suppress_context__ = True
        return _preserve_outcome(error, not_found_projected)

    if reason is BackendErrorReason.SOURCE_NOT_FOUND:
        source_id = _optional(error, diagnostics, "source_id", str)
        if source_id is None:
            raise BackendContractError(
                "source-not-found compatibility error lacks source_id",
                operation=error.operation,
            )
        method_id = cast(str | None, _optional(error, diagnostics, "method_id", str))
        if method_id is None and error.operation is not None:
            method_id = _SOURCE_NOT_FOUND_OPERATION_METHOD_IDS.get(error.operation)
        return _preserve_outcome(
            error,
            SourceNotFoundError(
                cast(str, source_id),
                method_id=method_id,
                raw_response=cast(
                    str | None,
                    _optional(error, diagnostics, "raw_response", str),
                ),
            ),
        )
    if reason is BackendErrorReason.NOTEBOOK_LIMIT:
        current_count = _required_int(error, diagnostics, "current_count")
        if current_count is None:
            raise BackendContractError(
                "notebook-limit compatibility error lacks current_count",
                operation=error.operation,
            )
        original = _project_original_rpc_error(error, diagnostics, label="notebook-limit")
        limit_projected = NotebookLimitError(
            current_count,
            limit=_required_int(error, diagnostics, "limit"),
            original_error=original,
        )
        # Legacy quota diagnosis raised the domain error directly from the
        # rejected CREATE_NOTEBOOK RPC. Preserve both the structured field and
        # the traceback relationship without replaying any arbitrary exception.
        limit_projected.__cause__ = original
        limit_projected.__context__ = original
        limit_projected.__suppress_context__ = True
        return _preserve_outcome(error, limit_projected)
    if reason is BackendErrorReason.LABEL_NOT_FOUND:
        kind = _label_kind(error, diagnostics)
        label_id = _optional(error, diagnostics, "label_id", str)
        if label_id is None:
            raise BackendContractError(
                "label-not-found compatibility error lacks label_id",
                operation=error.operation,
            )
        method_id = cast(str | None, _optional(error, diagnostics, "method_id", str))
        if method_id is None:
            # P9.2: a service-owned workflow names the phase whose read proved
            # the group absent; the legacy ``method_id`` is a projector concern.
            method_id = _LABEL_NOT_FOUND_PHASE_METHOD_IDS.get(
                cast(str | None, _optional(error, diagnostics, "phase", str))
            )
        # A collection is a label with a distinct discriminator, so one neutral
        # reason carries both domains; the discriminator picks the exact public
        # class each facade documented before the migration.
        not_found = (
            CollectionNotFoundError(cast(str, label_id), method_id=method_id)
            if kind is LabelKind.COLLECTION
            else LabelNotFoundError(cast(str, label_id), method_id=method_id)
        )
        return _preserve_outcome(error, not_found)
    if reason is BackendErrorReason.LABEL_AMBIGUOUS_CREATE:
        kind = _label_kind(error, diagnostics)
        ambiguity = (
            CollectionError(error.message)
            if kind is LabelKind.COLLECTION
            else LabelError(error.message)
        )
        return _preserve_outcome(error, ambiguity)

    if reason is BackendErrorReason.RESEARCH_START_UNAVAILABLE:
        notebook_id = _optional(error, diagnostics, "notebook_id", str)
        mode = _optional(error, diagnostics, "mode", str)
        if notebook_id is None or mode is None:
            raise BackendContractError(
                "research-start-unavailable compatibility error lacks notebook_id/mode",
                operation=error.operation,
            )
        original = _project_original_rpc_error(
            error, diagnostics, label="research-start-unavailable"
        )
        start_projected = ResearchStartUnavailableError(
            cast(str, notebook_id),
            cast(str, mode),
            method_id=original.method_id,
            raw_response=original.raw_response,
            rpc_code=original.rpc_code,
            found_ids=cast("list[str] | None", original.found_ids or None),
        )
        # Legacy diagnosis raised this domain error directly from the rejecting
        # start RPC. Preserve that traceback relationship without replaying any
        # arbitrary exception.
        start_projected.__cause__ = original
        start_projected.__context__ = original
        start_projected.__suppress_context__ = True
        return _preserve_outcome(error, start_projected)

    rpc = _rpc_diagnostics(error)
    if reason is BackendErrorReason.AUTH:
        projected = AuthError(error.message, **rpc)
        recoverable = _optional(error, diagnostics, "recoverable", bool)
        projected.recoverable = bool(recoverable)
        return _preserve_outcome(error, projected)
    if reason is BackendErrorReason.CLIENT:
        return _preserve_outcome(
            error,
            ClientError(
                error.message,
                status_code=_required_int(error, diagnostics, "status_code"),
                **rpc,
            ),
        )
    if reason is BackendErrorReason.NOT_FOUND:
        return _preserve_outcome(
            error,
            ClientError(
                error.message,
                status_code=_required_int(error, diagnostics, "status_code"),
                **rpc,
            ),
        )
    if reason is BackendErrorReason.DECODING:
        return _preserve_outcome(error, DecodingError(error.message, **rpc))
    if reason is BackendErrorReason.RATE_LIMIT:
        return _preserve_outcome(
            error,
            RateLimitError(
                error.message,
                retry_after=_required_int(error, diagnostics, "retry_after"),
                **rpc,
            ),
        )
    if reason is BackendErrorReason.RESPONSE_TOO_LARGE:
        return _preserve_outcome(
            error,
            RPCResponseTooLargeError(
                error.message,
                limit_bytes=_required_int(error, diagnostics, "limit_bytes"),
                bytes_read=_required_int(error, diagnostics, "bytes_read"),
                method_id=rpc["method_id"],
            ),
        )
    if reason is BackendErrorReason.RPC:
        return _preserve_outcome(error, RPCError(error.message, **rpc))
    if reason is BackendErrorReason.SERVER:
        return _preserve_outcome(
            error,
            ServerError(
                error.message,
                status_code=_required_int(error, diagnostics, "status_code"),
                **rpc,
            ),
        )
    raise BackendContractError(
        f"unsupported backend compatibility reason {reason.value!r}",
        operation=error.operation,
    )


def project_local_not_found(operation: Operation, resource_id: str) -> NotFoundError:
    """Project an optional-lookup miss without exposing native IDs to its caller.

    ``get_or_none`` operations deliberately return ``None`` for absence. Their
    public ``get`` facades must turn that local miss back into the historical
    ``*NotFoundError`` including its native method diagnostic, but a migrated
    facade must not import :class:`RPCMethod` to do it. Keep that legacy-only
    mapping beside the closed backend-error projector.
    """
    reason: BackendErrorReason
    diagnostics: dict[str, object]
    if operation is Operation.LABEL_GET:
        reason = BackendErrorReason.LABEL_NOT_FOUND
        diagnostics = {
            "label_id": resource_id,
            "label_kind": LabelKind.SOURCE_LABEL.value,
            "method_id": RPCMethod.LIST_LABELS.value,
        }
    elif operation is Operation.COLLECTION_GET:
        reason = BackendErrorReason.LABEL_NOT_FOUND
        diagnostics = {
            "label_id": resource_id,
            "label_kind": LabelKind.COLLECTION.value,
            "method_id": RPCMethod.LIST_LABELS.value,
        }
    elif operation is Operation.ARTIFACT_GET:
        reason = BackendErrorReason.ARTIFACT_NOT_FOUND
        diagnostics = {
            "artifact_id": resource_id,
            "method_id": RPCMethod.LIST_ARTIFACTS.value,
        }
    else:
        raise BackendContractError(
            f"operation {operation.value!r} has no local not-found compatibility contract",
            operation=operation,
        )

    projected = project_backend_error(
        BackendError(
            f"{operation.value} resource not found: {resource_id}",
            operation=operation,
            diagnostics=diagnostics,
            reason=reason,
        )
    )
    if not isinstance(projected, NotFoundError):
        raise BackendContractError(
            f"operation {operation.value!r} projected a non-not-found compatibility error",
            operation=operation,
        )
    return projected


async def project_backend_call(awaitable: Awaitable[_T]) -> _T:
    """Await one backend call and raise its projection outside the private handler."""
    public_error: Exception | None = None
    try:
        return await awaitable
    except BackendError as error:
        public_error = project_backend_error(error)
    assert public_error is not None
    raise public_error


def _project_source_add_record(record: SourceAddFailureRecord) -> Exception:
    original_error = (
        _project_source_add_record(record.original_error)
        if record.original_error is not None
        else None
    )
    cause = (
        original_error
        if record.cause_is_original
        else (_project_source_add_record(record.cause) if record.cause is not None else None)
    )
    if record.cause_original_is_original_error:
        if original_error is None or not isinstance(
            cause,
            (TransportAuthExpired, TransportRateLimited, TransportServerError),
        ):
            raise BackendContractError("failure graph has invalid shared transport original")
        cause.original = original_error
        if isinstance(original_error, httpx.HTTPStatusError) and isinstance(
            cause, (TransportRateLimited, TransportServerError)
        ):
            cause.response = original_error.response
    context = (
        cause
        if record.context_is_cause
        else (
            original_error
            if record.context_is_original
            else (
                _project_source_add_record(record.context) if record.context is not None else None
            )
        )
    )
    rpc: dict[str, Any] = {
        "method_id": record.method_id,
        "raw_response": record.raw_response,
        "rpc_code": record.rpc_code,
        "found_ids": list(record.found_ids),
    }
    kind = record.kind
    if kind is SourceAddFailureKind.SOURCE_ADD:
        if record.url is None:
            raise BackendContractError("source-add failure lacks url")
        projected: Exception = SourceAddError(record.url, cause=cause, message=record.message)
    elif kind is SourceAddFailureKind.SOURCE_NOT_FOUND:
        if record.source_id is None:
            raise BackendContractError("source-not-found failure lacks source_id")
        projected = SourceNotFoundError(
            record.source_id,
            method_id=rpc["method_id"],
            raw_response=record.raw_response,
        )
    elif kind is SourceAddFailureKind.VALIDATION:
        projected = ValidationError(*record.args)
    elif kind is SourceAddFailureKind.NON_IDEMPOTENT_RETRY:
        projected = NonIdempotentRetryError(*record.args)
    elif kind is SourceAddFailureKind.IDEMPOTENCY_VARIANT:
        projected = IdempotencyVariantError(*record.args)
    elif kind is SourceAddFailureKind.SOURCE_PROCESSING:
        if record.source_id is None or record.status is None:
            raise BackendContractError("source-processing failure lacks source_id/status")
        projected = SourceProcessingError(record.source_id, record.status, record.message)
    elif kind is SourceAddFailureKind.SOURCE_TIMEOUT:
        if record.source_id is None or record.timeout is None:
            raise BackendContractError("source-timeout failure lacks source_id/timeout")
        projected = SourceTimeoutError(record.source_id, record.timeout, record.last_status)
    elif kind is SourceAddFailureKind.AUTH:
        projected = AuthError(record.message, **rpc)
        projected.recoverable = bool(record.recoverable)
    elif kind is SourceAddFailureKind.CHAT:
        projected = ChatError(record.message)
    elif kind is SourceAddFailureKind.CHAT_RESPONSE_PARSE:
        projected = ChatResponseParseError(record.message)
    elif kind is SourceAddFailureKind.CLIENT:
        projected = ClientError(record.message, status_code=record.status_code, **rpc)
    elif kind is SourceAddFailureKind.DECODING:
        projected = DecodingError(record.message, **rpc)
    elif kind is SourceAddFailureKind.NETWORK:
        projected = NetworkError(
            record.message,
            method_id=rpc["method_id"],
            original_error=original_error,
        )
    elif kind is SourceAddFailureKind.RATE_LIMIT:
        projected = RateLimitError(
            record.message,
            retry_after=record.retry_after,
            **rpc,
        )
    elif kind is SourceAddFailureKind.RESPONSE_TOO_LARGE:
        projected = RPCResponseTooLargeError(
            record.message,
            limit_bytes=record.limit_bytes,
            bytes_read=record.bytes_read,
            method_id=rpc["method_id"],
        )
    elif kind is SourceAddFailureKind.RPC:
        projected = RPCError(record.message, **rpc)
    elif kind is SourceAddFailureKind.RPC_TIMEOUT:
        projected = RPCTimeoutError(
            record.message,
            timeout_seconds=record.timeout_seconds,
            method_id=rpc["method_id"],
            original_error=original_error,
        )
    elif kind is SourceAddFailureKind.SERVER:
        projected = ServerError(record.message, status_code=record.status_code, **rpc)
    elif kind is SourceAddFailureKind.UNKNOWN_RPC_METHOD:
        projected = UnknownRPCMethodError(
            record.message,
            method_id=record.method_id,
            path=record.path,
            source=record.source,
            found_ids=list(record.found_ids) or None,
            raw_response=record.raw_response,
            data_at_failure=record.data_at_failure,
            rpc_code=record.rpc_code,
        )
    else:
        status_projected: Exception | None
        if kind is SourceAddFailureKind.HTTPX_STATUS:
            if (
                record.request_method is None
                or record.request_url is None
                or record.status_code is None
            ):
                raise BackendContractError("httpx status failure has incomplete response evidence")
            status_request = httpx.Request(record.request_method, record.request_url)
            status_projected = httpx.HTTPStatusError(
                record.message,
                request=status_request,
                response=httpx.Response(record.status_code, request=status_request),
            )
        else:
            status_projected = None
        httpx_types: dict[SourceAddFailureKind, type[httpx.RequestError]] = {
            SourceAddFailureKind.HTTPX_REQUEST: httpx.RequestError,
            SourceAddFailureKind.HTTPX_TRANSPORT: httpx.TransportError,
            SourceAddFailureKind.HTTPX_TIMEOUT: httpx.TimeoutException,
            SourceAddFailureKind.HTTPX_CONNECT_TIMEOUT: httpx.ConnectTimeout,
            SourceAddFailureKind.HTTPX_READ_TIMEOUT: httpx.ReadTimeout,
            SourceAddFailureKind.HTTPX_WRITE_TIMEOUT: httpx.WriteTimeout,
            SourceAddFailureKind.HTTPX_POOL_TIMEOUT: httpx.PoolTimeout,
            SourceAddFailureKind.HTTPX_NETWORK: httpx.NetworkError,
            SourceAddFailureKind.HTTPX_CONNECT: httpx.ConnectError,
            SourceAddFailureKind.HTTPX_READ: httpx.ReadError,
            SourceAddFailureKind.HTTPX_WRITE: httpx.WriteError,
            SourceAddFailureKind.HTTPX_CLOSE: httpx.CloseError,
            SourceAddFailureKind.HTTPX_PROXY: httpx.ProxyError,
            SourceAddFailureKind.HTTPX_PROTOCOL: httpx.ProtocolError,
            SourceAddFailureKind.HTTPX_LOCAL_PROTOCOL: httpx.LocalProtocolError,
            SourceAddFailureKind.HTTPX_REMOTE_PROTOCOL: httpx.RemoteProtocolError,
            SourceAddFailureKind.HTTPX_UNSUPPORTED_PROTOCOL: httpx.UnsupportedProtocol,
            SourceAddFailureKind.HTTPX_TOO_MANY_REDIRECTS: httpx.TooManyRedirects,
            SourceAddFailureKind.HTTPX_DECODING: httpx.DecodingError,
        }
        httpx_type = httpx_types.get(kind)
        if status_projected is not None:
            projected = status_projected
        elif httpx_type is not None:
            if (record.request_method is None) != (record.request_url is None):
                raise BackendContractError("httpx failure has incomplete request evidence")
            request = (
                httpx.Request(record.request_method, record.request_url)
                if record.request_method is not None and record.request_url is not None
                else None
            )
            projected = httpx_type(record.message, request=request)
        elif kind is SourceAddFailureKind.TRANSPORT_AUTH_EXPIRED:
            if original_error is None:
                raise BackendContractError("transport auth failure lacks original error")
            projected = TransportAuthExpired(record.message, original=original_error)
        elif kind is SourceAddFailureKind.TRANSPORT_RATE_LIMITED:
            if not isinstance(original_error, httpx.HTTPStatusError):
                raise BackendContractError("transport rate-limit failure lacks HTTP status error")
            projected = TransportRateLimited(
                record.message,
                retry_after=record.retry_after,
                response=original_error.response,
                original=original_error,
            )
        elif kind is SourceAddFailureKind.TRANSPORT_SERVER:
            if original_error is None:
                raise BackendContractError("transport server failure lacks original error")
            projected = TransportServerError(
                record.message,
                original=original_error,
                response=(
                    original_error.response
                    if isinstance(original_error, httpx.HTTPStatusError)
                    else None
                ),
                status_code=record.status_code,
            )
        else:
            builtin_types: dict[SourceAddFailureKind, type[Exception]] = {
                SourceAddFailureKind.BUILTIN_CONNECTION: ConnectionError,
                SourceAddFailureKind.BUILTIN_BROKEN_PIPE: BrokenPipeError,
                SourceAddFailureKind.BUILTIN_CONNECTION_ABORTED: ConnectionAbortedError,
                SourceAddFailureKind.BUILTIN_CONNECTION_REFUSED: ConnectionRefusedError,
                SourceAddFailureKind.BUILTIN_CONNECTION_RESET: ConnectionResetError,
                SourceAddFailureKind.BUILTIN_OS: OSError,
                SourceAddFailureKind.BUILTIN_INDEX: IndexError,
                SourceAddFailureKind.BUILTIN_KEY: KeyError,
                SourceAddFailureKind.BUILTIN_RUNTIME: RuntimeError,
                SourceAddFailureKind.BUILTIN_TIMEOUT: TimeoutError,
                SourceAddFailureKind.BUILTIN_TYPE: TypeError,
                SourceAddFailureKind.BUILTIN_VALUE: ValueError,
            }
            builtin = builtin_types.get(kind)
            if builtin is None:
                raise BackendContractError(f"unsupported source-add failure kind {kind.value!r}")
            projected = builtin(*record.args)

    if record.source_id is not None and not hasattr(projected, "source_id"):
        projected.source_id = record.source_id  # type: ignore[attr-defined]
    if record.stage is not None:
        projected.stage = record.stage  # type: ignore[attr-defined]
    if record.unconfirmed:
        projected.unconfirmed = True  # type: ignore[attr-defined]
    projected.__cause__ = cause if record.explicit_cause else None
    projected.__context__ = context
    projected.__suppress_context__ = record.suppress_context
    return projected


def project_source_add_failure(record: SourceAddFailureRecord) -> Exception:
    """Reconstruct one positional batch-source failure record."""
    return _project_source_add_record(record)


__all__ = [
    "project_backend_call",
    "project_backend_error",
    "project_local_not_found",
    "project_source_add_failure",
]
