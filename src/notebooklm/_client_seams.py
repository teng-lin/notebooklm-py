"""Client-level late-rebind seams.

The callable seams in this module are intentionally separate from construction
seams such as ``async_client_factory``. ``ClientSeams`` owns only callables that
runtime closures may re-read after construction.
"""

from __future__ import annotations

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
    """Resolve sleep through ``notebooklm._session`` for patch compatibility."""
    from . import _session as session_mod

    return session_mod.asyncio.sleep


def _default_decode_response() -> Callable[..., Any]:
    """Resolve the canonical RPC response decoder."""
    from .rpc import decode_response

    return decode_response


def _default_is_auth_error() -> Callable[[Exception], bool]:
    """Resolve the canonical auth-error classifier."""
    from ._session_helpers import is_auth_error

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
