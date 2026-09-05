"""Import-light, owner-specific construction options for :class:`NotebookLMClient`."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypeAlias

from ._types.common import ConnectionLimits, CookieRotator, CookieSaver, RpcTelemetryEvent


def _positive_optional(value: float | None, *, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds or None (got {value!r})")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number or None (got {value!r})")


def _nonnegative(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer (got {value!r})")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


def _instance(value: object, expected: type[object], *, name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be {expected.__name__} (got {type(value).__name__})")


class AutoReadWindow(Enum):
    """Marker requesting NotebookLM's built-in, workload-scaled read window."""

    AUTO = "auto"

    def __repr__(self) -> str:
        return "AUTO"


AUTO = AutoReadWindow.AUTO
ReadWindow: TypeAlias = float | None | AutoReadWindow
RpcEventCallback: TypeAlias = Callable[[RpcTelemetryEvent], object]


@dataclass(frozen=True)
class RuntimeOptions:
    """Shared runtime capacity and optional whole-operation budget."""

    max_concurrent_rpcs: int | None = 16
    operation_timeout: float | None = None

    def __post_init__(self) -> None:
        if self.max_concurrent_rpcs is not None:
            if isinstance(self.max_concurrent_rpcs, bool) or not isinstance(
                self.max_concurrent_rpcs, int
            ):
                raise TypeError("max_concurrent_rpcs must be an integer or None")
            if self.max_concurrent_rpcs < 1:
                raise ValueError(
                    f"max_concurrent_rpcs must be >= 1, got {self.max_concurrent_rpcs!r}"
                )
        _positive_optional(self.operation_timeout, name="operation_timeout")


@dataclass(frozen=True)
class RetryOptions:
    """Automatic retry ceilings; backends still classify wire evidence."""

    rate_limit_max_retries: int = 3
    server_error_max_retries: int = 3

    def __post_init__(self) -> None:
        _nonnegative(self.rate_limit_max_retries, name="rate_limit_max_retries")
        _nonnegative(self.server_error_max_retries, name="server_error_max_retries")


@dataclass(frozen=True)
class WebTransportOptions:
    """HTTP transport settings owned by the Web backend."""

    read_timeout: float | None = 30.0
    write_timeout: float | None = 30.0
    pool_timeout: float | None = 30.0
    limits: ConnectionLimits = field(default_factory=ConnectionLimits)

    def __post_init__(self) -> None:
        _instance(self.limits, ConnectionLimits, name="limits")
        _positive_optional(self.read_timeout, name="read_timeout")
        _positive_optional(self.write_timeout, name="write_timeout")
        _positive_optional(self.pool_timeout, name="pool_timeout")


@dataclass(frozen=True)
class WebSessionOptions:
    """Cookie-session keepalive cadence owned by the Web session."""

    keepalive_interval: float | None = None
    keepalive_min_interval: float = 60.0

    def __post_init__(self) -> None:
        _positive_optional(self.keepalive_interval, name="keepalive_interval")
        _positive_optional(self.keepalive_min_interval, name="keepalive_min_interval")


@dataclass(frozen=True)
class WebSessionHooks:
    """Advanced Web cookie persistence hooks retained for the 0.x contract."""

    cookie_saver: CookieSaver | None = None
    cookie_rotator: CookieRotator | None = None


@dataclass(frozen=True)
class WebBackendConfig:
    """Construction specification for the Web backend."""

    kind: Literal["web"] = "web"
    transport: WebTransportOptions = field(default_factory=WebTransportOptions)
    session: WebSessionOptions = field(default_factory=WebSessionOptions)
    hooks: WebSessionHooks | None = None

    def __post_init__(self) -> None:
        if self.kind != "web":
            raise ValueError("WebBackendConfig.kind must be 'web'")
        _instance(self.transport, WebTransportOptions, name="transport")
        _instance(self.session, WebSessionOptions, name="session")
        if self.hooks is not None:
            _instance(self.hooks, WebSessionHooks, name="hooks")


@dataclass(frozen=True)
class AndroidBackendConfig:
    """Construction specification for the Android backend."""

    kind: Literal["android"] = "android"
    rpc_timeout: float | None = 30.0

    def __post_init__(self) -> None:
        if self.kind != "android":
            raise ValueError("AndroidBackendConfig.kind must be 'android'")
        _positive_optional(self.rpc_timeout, name="rpc_timeout")


@dataclass(frozen=True)
class TimeoutOptions:
    """Four independent HTTP timeout components for one transfer phase."""

    connect: float | None
    read: float | None
    write: float | None
    pool: float | None

    def __post_init__(self) -> None:
        for name in ("connect", "read", "write", "pool"):
            _positive_optional(getattr(self, name), name=name)


@dataclass(frozen=True)
class TransferOptions:
    """Upload capacity and phase-specific HTTP transfer windows."""

    max_concurrent_uploads: int = 4
    start_timeout: TimeoutOptions | None = None
    finalize_timeout: TimeoutOptions | None = None
    drive_timeout: TimeoutOptions | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_concurrent_uploads, bool) or not isinstance(
            self.max_concurrent_uploads, int
        ):
            raise TypeError("max_concurrent_uploads must be an integer")
        if self.max_concurrent_uploads < 1:
            raise ValueError(
                f"max_concurrent_uploads must be >= 1, got {self.max_concurrent_uploads!r}"
            )
        for name in ("start_timeout", "finalize_timeout", "drive_timeout"):
            value = getattr(self, name)
            if value is not None:
                _instance(value, TimeoutOptions, name=name)


@dataclass(frozen=True)
class FeatureOptions:
    """Feature-owned read windows and buffered response limit."""

    chat_timeout: ReadWindow = AUTO
    chat_response_max_bytes: int | None = 256 * 1024 * 1024
    import_research_timeout: ReadWindow = AUTO

    def __post_init__(self) -> None:
        for name in ("chat_timeout", "import_research_timeout"):
            value = getattr(self, name)
            if value is AUTO or value is None:
                continue
            _positive_optional(value, name=name)
        if self.chat_response_max_bytes is not None:
            if isinstance(self.chat_response_max_bytes, bool) or not isinstance(
                self.chat_response_max_bytes, int
            ):
                raise TypeError("chat_response_max_bytes must be an integer or None")
            if self.chat_response_max_bytes < 1:
                raise ValueError(
                    "chat_response_max_bytes must be >= 1 when supplied "
                    f"(got {self.chat_response_max_bytes!r})"
                )


@dataclass(frozen=True)
class ClientConfig:
    """Complete frozen client construction specification."""

    backend: WebBackendConfig | AndroidBackendConfig | None = None
    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)
    retry: RetryOptions = field(default_factory=RetryOptions)
    transfers: TransferOptions = field(default_factory=TransferOptions)
    features: FeatureOptions = field(default_factory=FeatureOptions)
    on_rpc_event: RpcEventCallback | None = None

    def __post_init__(self) -> None:
        if self.backend is not None and not isinstance(
            self.backend, (WebBackendConfig, AndroidBackendConfig)
        ):
            raise TypeError("backend must be WebBackendConfig, AndroidBackendConfig, or None")
        _instance(self.runtime, RuntimeOptions, name="runtime")
        _instance(self.retry, RetryOptions, name="retry")
        _instance(self.transfers, TransferOptions, name="transfers")
        _instance(self.features, FeatureOptions, name="features")
        if self.on_rpc_event is not None and not callable(self.on_rpc_event):
            raise TypeError("on_rpc_event must be callable or None")


__all__ = [
    "AndroidBackendConfig",
    "AUTO",
    "AutoReadWindow",
    "ClientConfig",
    "FeatureOptions",
    "ReadWindow",
    "RetryOptions",
    "RpcEventCallback",
    "RuntimeOptions",
    "TimeoutOptions",
    "TransferOptions",
    "WebBackendConfig",
    "WebSessionHooks",
    "WebSessionOptions",
    "WebTransportOptions",
]
