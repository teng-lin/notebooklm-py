"""Chat-domain consumer-side error-mapping seam over generic transport.

This module owns the chat-flavored exception mapping that wraps a
single authed POST attempt against the NotebookLM batchexecute
endpoint. It is the chat-domain consumer-side seam: transport-layer
exceptions (``_TransportAuthExpired``, ``_TransportRateLimited``,
``_TransportServerError``, raw ``httpx.HTTPStatusError``) raised by
``Session._perform_authed_post`` are translated into ``ChatError``
or ``NetworkError`` so callers (currently only :class:`ChatAPI.ask`)
stay free of HTTP-status branching.

After the D2 cutover (PR-2 / arch-d2-cutover), :meth:`ChatAPI.ask`
calls :func:`chat_aware_authed_post` directly, replacing the prior
chat-side wrapper that lived on the core's RPC executor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ._core_transport import (
    _TransportAuthExpired,
    _TransportRateLimited,
    _TransportServerError,
)
from .exceptions import ChatError, NetworkError

if TYPE_CHECKING:
    from ._request_types import BuildRequest
    from ._session_contracts import Session


async def chat_aware_authed_post(
    session: Session,
    *,
    build_request: BuildRequest,
    parse_label: str,
) -> httpx.Response:
    """Chat-side semantic owner around :meth:`Session.transport_post`.

    Wraps the shared transport pipeline with chat-flavored exception
    mapping: transport-layer auth failures become
    :class:`~notebooklm.exceptions.ChatError`, and transport-layer
    network/rate-limit failures become
    :class:`~notebooklm.exceptions.NetworkError` /
    :class:`~notebooklm.exceptions.ChatError` respectively. This keeps
    ChatAPI free of HTTP-status branching and matches the historical
    contract of ``ChatAPI.ask``, which calls this helper directly
    post-D2-cutover (replacing the prior wrapper on the core's RPC
    executor).

    Args:
        session: Shared client session.
        build_request: Request builder forwarded to :meth:`Session.transport_post`.
        parse_label: Caller-friendly label used in log lines and error
            messages (e.g. ``"chat.ask"``).
    """
    # Drain admission lives in ``DrainMiddleware`` at the outermost chain
    # position around ``_perform_authed_post`` — it reads ``log_label``
    # from ``RpcRequest.context`` (passed below as ``parse_label``), so a
    # drained client still surfaces ``RuntimeError`` with the chat-friendly
    # label without explicit bracketing here.
    try:
        return await session.transport_post(
            build_request=build_request,
            parse_label=parse_label,
        )
    except _TransportAuthExpired as exc:
        raise ChatError(
            f"{parse_label} failed: authentication expired and refresh did not recover"
        ) from exc
    except _TransportRateLimited as exc:
        raise ChatError(
            f"{parse_label} rate-limited (HTTP 429)."
            + (f" Retry after {exc.retry_after} seconds." if exc.retry_after is not None else "")
        ) from exc
    except _TransportServerError as exc:
        if isinstance(exc.original, httpx.HTTPStatusError):
            raise ChatError(
                f"{parse_label} failed with HTTP {exc.original.response.status_code} "
                f"after retries: {exc.original}"
            ) from exc
        # Network-layer failure (RequestError / Timeout).
        # ``_perform_authed_post`` only wraps ``httpx.RequestError`` into
        # ``_TransportServerError`` on the network path; this guard keeps
        # the contract enforced under ``python -O`` (where ``assert``
        # would be stripped) and gives a clear diagnostic if the
        # invariant ever drifts.
        if not isinstance(exc.original, httpx.RequestError):
            raise TypeError(
                f"Unexpected _TransportServerError.original type: {type(exc.original)}. "
                "Expected httpx.HTTPStatusError or httpx.RequestError."
            ) from exc
        # Preserve the timeout-specific message: TimeoutException is a
        # subclass of RequestError, so without this branch read/connect
        # timeouts would surface as a generic "network error after
        # retries" line and lose the "timed out" signal callers rely on.
        if isinstance(exc.original, httpx.TimeoutException):
            raise NetworkError(
                f"{parse_label} timed out after retries: {exc.original}",
                original_error=exc.original,
            ) from exc
        raise NetworkError(
            f"{parse_label} network error after retries: {exc.original}",
            original_error=exc.original,
        ) from exc
    except httpx.HTTPStatusError as exc:
        # Non-5xx / non-401 / non-429 status errors fall through
        # ``_perform_authed_post``'s "Anything else" branch (e.g. a 404
        # or unhandled 4xx).
        raise ChatError(
            f"{parse_label} failed with HTTP {exc.response.status_code}: {exc}"
        ) from exc
    # NOTE: bare ``httpx.TimeoutException`` / ``httpx.RequestError``
    # handlers were removed here because ``_perform_authed_post`` always
    # either retries those errors or wraps them in
    # ``_TransportServerError`` (handled above), so they cannot reach
    # this scope.
