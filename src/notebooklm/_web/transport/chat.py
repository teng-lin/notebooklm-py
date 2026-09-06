"""Web chat consumer-side error-mapping seam over generic transport.

This module owns the chat-flavored exception mapping that wraps a
single authed POST attempt against the NotebookLM batchexecute
endpoint. It is the chat-domain consumer-side seam: transport-layer
exceptions (``TransportAuthExpired``, ``TransportRateLimited``,
``TransportServerError``, raw ``httpx.HTTPStatusError``) raised by
:meth:`RuntimeTransport.perform_authed_post` are translated into
``ChatError`` or ``NetworkError`` so callers (currently only
:class:`ChatAPI.ask`) stay free of HTTP-status branching.

:meth:`ChatAPI.ask` calls :func:`chat_aware_authed_post` directly. Per
ADR-0014 Rule 2 Corollary, this helper takes the :class:`RuntimeTransport`
collaborator directly rather than a local ``ChatRuntime`` Protocol — the
indirection through a chat-local Protocol added no value once
``transport_post`` was its only member.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ..._env import get_default_bl
from ..._idempotency import (
    JournalEntry,
    attach_journal_entry,
    bind_operation_journal_entries,
    bound_operation_journal_entry,
    mark_commit_state,
    mark_unconfirmed,
)
from ...exceptions import ChatError, NetworkError, RPCResponseTooLargeError
from ...outcomes import CommitState
from .errors import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
)

if TYPE_CHECKING:
    from .request_types import BuildRequest
    from .runtime import RuntimeTransport


_ZERO_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _mark_chat_failure(
    error: Exception,
    original: Exception | None = None,
    journal_entry: JournalEntry | None = None,
) -> Exception:
    """Attach conservative chat-send evidence to the escaping public error."""
    if isinstance(original, _ZERO_SEND_ERRORS):
        mark_commit_state(error, CommitState.NOT_SENT, operation="chat")
        if journal_entry is not None:
            journal_entry.record(CommitState.NOT_SENT, "verified pre-send failure")
    else:
        mark_unconfirmed(error, operation="chat")
    if journal_entry is not None:
        attach_journal_entry(error, journal_entry)
    return error


def _format_chat_read_timeout_message(
    *,
    parse_label: str,
    read_timeout: float | None,
    original: httpx.ReadTimeout,
) -> str:
    timeout_label = (
        f"{read_timeout:g}s" if read_timeout is not None else "the configured read timeout"
    )
    original_text = str(original).strip()
    original_suffix = f": {original_text}" if original_text else ""
    return (
        f"{parse_label} received no streamed chat bytes for {timeout_label}{original_suffix}. "
        "This points to a server slow-to-first-byte or between-chunk chat-stream stall, "
        "which is common on shared notebooks, not a batchexecute byte-count mismatch. "
        f"Active NOTEBOOKLM_BL={get_default_bl()!r}. "
        "If you overrode the timeout lower, rerun with --request-timeout 180; "
        "otherwise try a higher value and compare owner vs viewer access."
    )


async def chat_aware_authed_post(
    transport: RuntimeTransport,
    *,
    build_request: BuildRequest,
    parse_label: str,
    read_timeout: float | None = None,
    max_response_bytes: int | None = None,
    disable_read_timeout_retries: bool = False,
) -> httpx.Response:
    """Chat-side semantic owner around :meth:`RuntimeTransport.perform_authed_post`.

    Wraps the shared transport pipeline with chat-flavored exception
    mapping: transport-layer auth failures become
    :class:`~notebooklm.exceptions.ChatError`, and transport-layer
    network/rate-limit failures become
    :class:`~notebooklm.exceptions.NetworkError` /
    :class:`~notebooklm.exceptions.ChatError` respectively. This keeps
    ChatAPI free of HTTP-status branching. ``ChatAPI.ask`` calls this
    helper directly.

    Args:
        transport: :class:`RuntimeTransport` collaborator that owns the
            authed POST entry point on the shared transport pipeline.
            Passed directly via constructor injection from
            ``NotebookLMClient.__init__`` (ADR-0014 Rule 2 Corollary) — no
            chat-local Protocol intermediates.
        build_request: Request builder forwarded to
            :meth:`RuntimeTransport.perform_authed_post`.
        parse_label: Caller-friendly label used in log lines and error
            messages (e.g. ``"chat.ask"``). Threaded through to the
            transport as ``log_label`` — the two names refer to the same
            value (``parse_label`` is the chat-domain spelling; the chain
            context still names it ``log_label``).
        max_response_bytes: Optional per-call response-size cap forwarded to
            the shared streaming transport.
    """
    journal_entry = bound_operation_journal_entry()
    # ``CallSupervisor`` wraps ``perform_authed_post`` and receives this
    # chat-friendly label, so admission failures retain useful diagnostics
    # without explicit bracketing here.
    try:
        with bind_operation_journal_entries(journal_entry):
            return await transport.perform_authed_post(
                build_request=build_request,
                log_label=parse_label,
                read_timeout=read_timeout,
                max_response_bytes=max_response_bytes,
                disable_read_timeout_retries=disable_read_timeout_retries,
            )
    except TransportAuthExpired as exc:
        error = ChatError(
            f"{parse_label} failed: authentication expired and refresh did not recover"
        )
        raise _mark_chat_failure(error, exc.original, journal_entry) from exc
    except TransportRateLimited as exc:
        error = ChatError(
            f"{parse_label} rate-limited (HTTP 429)."
            + (f" Retry after {exc.retry_after} seconds." if exc.retry_after is not None else "")
        )
        raise _mark_chat_failure(error, exc.original, journal_entry) from exc
    except TransportServerError as exc:
        if isinstance(exc.original, httpx.HTTPStatusError):
            error = ChatError(
                f"{parse_label} failed with HTTP {exc.original.response.status_code}: "
                f"{exc.original}"
            )
            raise _mark_chat_failure(error, exc.original, journal_entry) from exc
        # Network-layer failure (RequestError / Timeout).
        # ``RuntimeTransport.perform_authed_post`` only wraps
        # ``httpx.RequestError`` into ``TransportServerError`` on the network path; this guard keeps
        # the contract enforced under ``python -O`` (where ``assert``
        # would be stripped) and gives a clear diagnostic if the
        # invariant ever drifts.
        if not isinstance(exc.original, httpx.RequestError):
            raise TypeError(
                f"Unexpected TransportServerError.original type: {type(exc.original)}. "
                "Expected httpx.HTTPStatusError or httpx.RequestError."
            ) from exc
        # Preserve timeout-specific messages: TimeoutException is a
        # subclass of RequestError, so without this branch read/connect
        # timeouts would surface as a generic "network error after
        # retries" line and lose the timeout signal callers rely on. A
        # streamed-chat read timeout is especially diagnostic: it means no
        # response bytes arrived for the HTTPX read window, either before
        # the first byte or between chunks.
        if isinstance(exc.original, httpx.ReadTimeout):
            network_error = NetworkError(
                _format_chat_read_timeout_message(
                    parse_label=parse_label,
                    read_timeout=read_timeout,
                    original=exc.original,
                ),
                original_error=exc.original,
            )
            raise _mark_chat_failure(network_error, exc.original, journal_entry) from exc
        if isinstance(exc.original, httpx.TimeoutException):
            network_error = NetworkError(
                f"{parse_label} timed out: {exc.original}",
                original_error=exc.original,
            )
            raise _mark_chat_failure(network_error, exc.original, journal_entry) from exc
        network_error = NetworkError(
            f"{parse_label} network error: {exc.original}",
            original_error=exc.original,
        )
        raise _mark_chat_failure(network_error, exc.original, journal_entry) from exc
    except httpx.HTTPStatusError as exc:
        # Non-5xx / non-401 / non-429 status errors fall through
        # ``RuntimeTransport.perform_authed_post``'s "Anything else"
        # branch (e.g. a 404 or unhandled 4xx).
        error = ChatError(f"{parse_label} failed with HTTP {exc.response.status_code}: {exc}")
        raise _mark_chat_failure(error, exc, journal_entry) from exc
    except RPCResponseTooLargeError as exc:
        mark_unconfirmed(exc, operation="chat")
        if journal_entry is not None:
            attach_journal_entry(exc, journal_entry)
        raise
    # NOTE: bare ``httpx.TimeoutException`` / ``httpx.RequestError``
    # handlers were removed here because the shared authed transport always
    # either retries those errors or wraps them in
    # ``TransportServerError`` (handled above), so they cannot reach
    # this scope.
