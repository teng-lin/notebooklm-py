"""Client-level late-rebind seams.

The callable seams in this module are intentionally separate from construction
seams such as ``async_client_factory``. ``ClientSeams`` owns only callables that
runtime closures may re-read after construction.

**TEST-ONLY injection points.** The three ``ClientSeams`` callables
(``decode_response`` / ``sleep`` / ``is_auth_error``) — along with the
construction-only ``async_client_factory`` resolved in ``_runtime_init`` —
are never varied in production: ``NotebookLMClient.__init__`` hardcodes
all four to ``None`` (``resolve_client_seams(decode_response=None,
sleep=None, is_auth_error=None)``), so they always resolve to the
canonical module bindings (:func:`_default_decode_response` /
:func:`_default_sleep` / :func:`_default_is_auth_error` /
:class:`httpx.AsyncClient`). The non-``None`` paths exist solely so tests
can inject deterministic substitutes via ``compose_client_internals`` or
the client-shell test helper. Do not wire a public ``NotebookLMClient``
kwarg to any of them without a production caller that varies them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ClientSeams:
    """Runtime callables re-read by RPC and middleware closures."""

    decode_response: Callable[..., Any]
    sleep: Callable[[float], Awaitable[Any]]
    is_auth_error: Callable[[Exception], bool]


def _default_sleep() -> Callable[[float], Awaitable[Any]]:
    """Resolve the default async sleep callable."""
    return asyncio.sleep


def _default_decode_response() -> Callable[..., Any]:
    """Resolve the canonical RPC response decoder."""
    from .rpc import decode_response

    return decode_response


def _default_is_auth_error() -> Callable[[Exception], bool]:
    """Resolve the canonical auth-error classifier."""
    from ._runtime_helpers import is_auth_error

    return is_auth_error


def resolve_client_seams(
    *,
    sleep: Callable[[float], Awaitable[Any]] | None,
    is_auth_error: Callable[[Exception], bool] | None,
    decode_response: Callable[..., Any] | None,
) -> ClientSeams:
    """Resolve ``None`` seam defaults into a mutable runtime holder."""
    return ClientSeams(
        decode_response=_default_decode_response() if decode_response is None else decode_response,
        sleep=_default_sleep() if sleep is None else sleep,
        is_auth_error=_default_is_auth_error() if is_auth_error is None else is_auth_error,
    )


__all__ = [
    "ClientSeams",
    "resolve_client_seams",
]
