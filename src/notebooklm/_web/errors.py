"""Shared native-to-neutral failure translation for the web backend.

``translate_web_error`` is the one place a reviewed transport exception
becomes a :class:`~notebooklm._backend.BackendError`.  ``WebRpcBackend``
delegates its ``_translate_error`` classmethod here, and binding rows whose
``map_error`` needs the shared translation of the failing native (the
``RESEARCH_START`` unavailable case) call it directly, so no row module has to
import the backend head.  This module carries no operation-specific sets: that
knowledge is row metadata.
"""

from __future__ import annotations

from types import MappingProxyType

from .._backend import BackendContractError, BackendError, BackendErrorReason
from .._operations import Operation
from ..exceptions import ChatError, IdempotencyVariantError, NetworkError, RPCError
from .error_policy import SAFE_REASON_DIAGNOSTICS, WEB_ERROR_REASONS
from .failure_projection import _CHAT_OPERATIONS, _capture_public_failure

WebNativeError = RPCError | NetworkError | IdempotencyVariantError | ChatError


def error_diagnostics(
    exc: WebNativeError,
    reason: BackendErrorReason,
) -> MappingProxyType[str, object]:
    """Return the scrubbed diagnostics one reviewed native failure contributes."""
    diagnostics = {
        "method_id": getattr(exc, "method_id", None),
        "rpc_code": getattr(exc, "rpc_code", None),
        "found_ids": getattr(exc, "found_ids", None),
        "raw_response": getattr(exc, "raw_response", None),
    }
    diagnostics.update((name, getattr(exc, name)) for name in SAFE_REASON_DIAGNOSTICS[reason])
    return MappingProxyType(diagnostics)


def translate_web_error(
    operation: Operation,
    exc: WebNativeError,
    *,
    scrub_request_urls: bool | None = None,
) -> BackendError:
    """Translate one reviewed native exception into the closed neutral error.

    ``scrub_request_urls`` is the row's ``ErrorMode.TRANSLATE_SCRUBBED`` projection;
    ``None`` keeps the shared translator's operation-specific default.
    """
    reason = WEB_ERROR_REASONS.get(type(exc))
    if reason is None:
        raise BackendContractError(
            f"unclassified web error type {type(exc).__module__}.{type(exc).__qualname__}",
            operation=operation,
        ) from exc
    diagnostics = dict(error_diagnostics(exc, reason))
    if isinstance(exc, (RPCError, NetworkError, ChatError)):
        diagnostics["public_error_failure"] = _capture_public_failure(
            exc,
            operation=operation,
            scrub_request_urls=(
                operation in _CHAT_OPERATIONS if scrub_request_urls is None else scrub_request_urls
            ),
        )
    return BackendError(
        # Structured subclasses such as UnknownRPCMethodError append their
        # diagnostic fields in ``__str__``. Store only the base message so
        # the compatibility projector can reattach those fields exactly
        # once instead of duplicating the rendered suffix.
        message=str(exc.args[0]) if exc.args else "",
        operation=operation,
        outcome_unknown=bool(getattr(exc, "unconfirmed", False)),
        diagnostics=MappingProxyType(diagnostics),
        reason=reason,
        # ``WebTransport`` tags every native failure that escaped the
        # runtime; the neutral commit-uncertainty predicate reads it here.
        dispatched=bool(getattr(exc, "dispatched", False)),
    )


__all__ = ["WebNativeError", "error_diagnostics", "translate_web_error"]
