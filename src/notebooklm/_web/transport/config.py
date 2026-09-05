"""Validated configuration owned by the Web transport graph."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..._runtime.config import normalize_max_concurrent_uploads
from ..._runtime.helpers import _resolve_keepalive_interval
from ..._runtime.init import SharedRuntimeConfig, validate_shared_runtime_config

if TYPE_CHECKING:
    from ...types import ConnectionLimits


@dataclass(frozen=True)
class WebSessionConfig:
    """Validated values read only by the Web runtime."""

    read_timeout: float | None
    write_timeout: float | None
    pool_timeout: float | None
    connect_timeout: float
    limits: ConnectionLimits
    refresh_retry_delay: float
    rate_limit_max_retries: int
    server_error_max_retries: int
    keepalive_interval: float | None
    keepalive_storage_path: Path | None
    decode_response: Callable[..., Any]
    sleep: Callable[[float], Awaitable[Any]]
    is_auth_error: Callable[[Exception], bool]
    async_client_factory: Callable[..., httpx.AsyncClient]


def validate_web_config(
    *,
    read_timeout: float | None,
    write_timeout: float | None,
    pool_timeout: float | None,
    connect_timeout: float,
    refresh_retry_delay: float,
    rate_limit_max_retries: int,
    server_error_max_retries: int,
    keepalive: float | None,
    keepalive_min_interval: float,
    keepalive_storage_path: Path | None,
    auth_storage_path: Path | None,
    limits: ConnectionLimits | None,
    max_concurrent_uploads: int | None,
    max_concurrent_rpcs: int | None,
    decode_response: Callable[..., Any],
    sleep: Callable[[float], Awaitable[Any]],
    is_auth_error: Callable[[Exception], bool],
    async_client_factory: Callable[..., httpx.AsyncClient],
    shared_config: SharedRuntimeConfig | None = None,
) -> tuple[WebSessionConfig, SharedRuntimeConfig]:
    """Validate Web fields in their historical order and return both configs."""

    if limits is not None:
        resolved_limits = limits
    else:
        from ...types import ConnectionLimits

        resolved_limits = ConnectionLimits()

    if rate_limit_max_retries < 0:
        raise ValueError(f"rate_limit_max_retries must be >= 0, got {rate_limit_max_retries}")
    if server_error_max_retries < 0:
        raise ValueError(f"server_error_max_retries must be >= 0, got {server_error_max_retries}")
    normalize_max_concurrent_uploads(max_concurrent_uploads)
    if shared_config is None:
        shared_config = validate_shared_runtime_config(
            max_concurrent_rpcs=max_concurrent_rpcs,
        )

    resolved_storage_path = (
        keepalive_storage_path if keepalive_storage_path is not None else auth_storage_path
    )
    return (
        WebSessionConfig(
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            pool_timeout=pool_timeout,
            connect_timeout=connect_timeout,
            limits=resolved_limits,
            refresh_retry_delay=refresh_retry_delay,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            keepalive_interval=_resolve_keepalive_interval(keepalive, keepalive_min_interval),
            keepalive_storage_path=resolved_storage_path,
            decode_response=decode_response,
            sleep=sleep,
            is_auth_error=is_auth_error,
            async_client_factory=async_client_factory,
        ),
        shared_config,
    )


__all__ = ["WebSessionConfig", "validate_web_config"]
