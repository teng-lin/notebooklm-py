"""Single compatibility boundary from flat client kwargs to typed owner options."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast

import httpx

from ._client_contracts import BackendName
from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._types.common import ConnectionLimits
from .options import (
    AndroidBackendConfig,
    ClientConfig,
    FeatureOptions,
    RetryOptions,
    RuntimeOptions,
    TimeoutOptions,
    TransferOptions,
    WebBackendConfig,
    WebSessionHooks,
    WebSessionOptions,
    WebTransportOptions,
)


@dataclass(frozen=True)
class BackendPreference:
    """One construction-time backend preference and how it was selected."""

    preferred: BackendName
    reason: Literal["explicit", "env", "default"]


def resolve_backend_preference(*, explicit: str | None, env: str | None) -> BackendPreference:
    """Resolve and validate the backend preference without performing I/O."""

    value: str
    reason: Literal["explicit", "env", "default"]
    if explicit is not None:
        value = explicit
        reason = "explicit"
    elif env is not None:
        value = env
        reason = "env"
    else:
        value = "web"
        reason = "default"
    if value not in ("web", "android"):
        raise ValueError(
            f"Invalid NotebookLM backend {value!r}: expected 'web' or 'android'. "
            "The aliases 'mobile' and 'auto' are not supported."
        )
    return BackendPreference(preferred=cast(BackendName, value), reason=reason)


@dataclass(frozen=True)
class NormalizedClientOptions:
    """Resolved public specification plus compatibility diagnostics."""

    config: ClientConfig
    preference: BackendPreference
    legacy_arguments: tuple[str, ...]
    ignored_web_arguments: tuple[str, ...]
    typed_config: bool


@dataclass
class _StorageConstructionContext:
    """Exact stored-auth class call whose frozen preference may be adopted."""

    preference: BackendPreference
    target_type: type[Any]
    target_auth: object
    target_instance: object | None = None
    consumed: bool = False
    allocation_depth: int = 0


_CONSTRUCTION_CONTEXT: ContextVar[_StorageConstructionContext | None] = ContextVar(
    "notebooklm_client_construction_context", default=None
)


@contextlib.contextmanager
def client_construction_context(
    preference: BackendPreference,
    *,
    target_type: type[Any],
    target_auth: object,
) -> Iterator[None]:
    """Carry a frozen preference for one stored-auth class call."""

    token: Token[_StorageConstructionContext | None] = _CONSTRUCTION_CONTEXT.set(
        _StorageConstructionContext(preference, target_type, target_auth)
    )
    try:
        yield
    finally:
        _CONSTRUCTION_CONTEXT.reset(token)


@contextlib.contextmanager
def storage_construction_allocation(
    client_type: type[Any],
    auth: object,
) -> Iterator[Callable[[object], None]]:
    """Let only the outer target class call claim its returned allocation.

    A subclass may define a cooperative or non-cooperative ``__new__`` and may
    recursively construct the same class with the same auth object. Wrapping
    each allocation scope lets the outer class call claim only after its own
    ``__new__`` returns, while nested calls remain ordinary constructions.
    """

    context = _CONSTRUCTION_CONTEXT.get()
    if (
        context is None
        or context.target_instance is not None
        or client_type is not context.target_type
        or auth is not context.target_auth
    ):
        yield _ignore_storage_construction_instance
        return

    is_outer_allocation = context.allocation_depth == 0
    context.allocation_depth += 1
    try:

        def claim(instance: object) -> None:
            if is_outer_allocation and context.target_instance is None:
                context.target_instance = instance

        yield claim
    finally:
        context.allocation_depth -= 1


def _ignore_storage_construction_instance(_instance: object) -> None:
    """Ignore an allocation outside the exact stored-auth target class call."""


def consume_storage_construction_preference(
    client: object,
    auth: object,
) -> BackendPreference | None:
    """Consume the frozen handoff only for its pre-claimed allocated instance.

    The outer object is claimed before its subclass initializer runs, so nested
    same-class and same-auth construction cannot inherit the preference or warning
    suppression, including when a custom ``__new__`` recursively allocates.
    """

    context = _CONSTRUCTION_CONTEXT.get()
    if context is None or context.consumed:
        return None
    if client is not context.target_instance or auth is not context.target_auth:
        return None
    context.consumed = True
    return context.preference


def _legacy_argument_names(
    *,
    timeout: object,
    keepalive: object,
    keepalive_min_interval: object,
    rate_limit_max_retries: object,
    server_error_max_retries: object,
    limits: object,
    max_concurrent_uploads: object,
    max_concurrent_rpcs: object,
    upload_timeout: object,
    on_rpc_event: object,
    cookie_saver: object,
    cookie_rotator: object,
    chat_timeout: object,
    chat_response_max_bytes: object,
    import_research_timeout: object,
    backend: object,
) -> tuple[str, ...]:
    values = {
        "timeout": (timeout, DEFAULT_TIMEOUT),
        "keepalive": (keepalive, None),
        "keepalive_min_interval": (keepalive_min_interval, DEFAULT_KEEPALIVE_MIN_INTERVAL),
        "rate_limit_max_retries": (rate_limit_max_retries, 3),
        "server_error_max_retries": (server_error_max_retries, 3),
        "limits": (limits, None),
        "max_concurrent_uploads": (
            max_concurrent_uploads,
            DEFAULT_MAX_CONCURRENT_UPLOADS,
        ),
        "max_concurrent_rpcs": (max_concurrent_rpcs, DEFAULT_MAX_CONCURRENT_RPCS),
        "upload_timeout": (upload_timeout, None),
        "on_rpc_event": (on_rpc_event, None),
        "cookie_saver": (cookie_saver, None),
        "cookie_rotator": (cookie_rotator, None),
        "chat_timeout": (chat_timeout, AUTO_READ_TIMEOUT),
        "chat_response_max_bytes": (
            chat_response_max_bytes,
            DEFAULT_CHAT_RESPONSE_MAX_BYTES,
        ),
        "import_research_timeout": (import_research_timeout, AUTO_READ_TIMEOUT),
        "backend": (backend, None),
    }
    return tuple(
        sorted(
            name
            for name, (value, default) in values.items()
            if value is not default and value != default
        )
    )


def legacy_client_option_names(**kwargs: object) -> tuple[str, ...]:
    """Return differing flat tuning names without constructing or validating options."""

    return _legacy_argument_names(**kwargs)


_T = TypeVar("_T")


def _legacy_instance(cls: type[_T], **values: object) -> _T:
    """Create frozen options without applying new validation to legacy accepted values."""

    instance = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


def _timeout_options(value: httpx.Timeout) -> TimeoutOptions:
    return _legacy_instance(
        TimeoutOptions,
        connect=value.connect,
        read=value.read,
        write=value.write,
        pool=value.pool,
    )


def _resolved_config(config: ClientConfig, preference: BackendPreference) -> ClientConfig:
    backend = config.backend
    if backend is None:
        backend = WebBackendConfig() if preference.preferred == "web" else AndroidBackendConfig()
    return ClientConfig(
        backend=backend,
        runtime=config.runtime,
        retry=config.retry,
        transfers=config.transfers,
        features=config.features,
        on_rpc_event=config.on_rpc_event,
    )


def normalize_legacy_client_options(
    *,
    timeout: float | ClientConfig = DEFAULT_TIMEOUT,
    keepalive: float | None = None,
    keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
    rate_limit_max_retries: int = 3,
    server_error_max_retries: int = 3,
    limits: Any = None,
    max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
    max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
    upload_timeout: httpx.Timeout | None = None,
    on_rpc_event: Any = None,
    cookie_saver: Any = None,
    cookie_rotator: Any = None,
    chat_timeout: Any = AUTO_READ_TIMEOUT,
    chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    import_research_timeout: Any = AUTO_READ_TIMEOUT,
    backend: BackendName | None = None,
    config: ClientConfig | None = None,
    preference: BackendPreference | None = None,
) -> NormalizedClientOptions:
    """Normalize every public flat tuning option at the sole compatibility boundary."""

    if config is not None and not isinstance(config, ClientConfig):
        raise TypeError("config must be a ClientConfig or None")
    if isinstance(timeout, ClientConfig):
        raise TypeError(
            "ClientConfig is keyword-only; pass it as config=ClientConfig(...) rather than "
            "in the legacy timeout position."
        )
    legacy_arguments = _legacy_argument_names(
        timeout=timeout,
        keepalive=keepalive,
        keepalive_min_interval=keepalive_min_interval,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        limits=limits,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=max_concurrent_rpcs,
        upload_timeout=upload_timeout,
        on_rpc_event=on_rpc_event,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        chat_timeout=chat_timeout,
        chat_response_max_bytes=chat_response_max_bytes,
        import_research_timeout=import_research_timeout,
        backend=backend,
    )
    if config is not None and legacy_arguments:
        joined = ", ".join(legacy_arguments)
        raise TypeError(
            f"config= cannot be combined with non-default legacy tuning arguments: {joined}"
        )

    if preference is None:
        explicit = (
            config.backend.kind if config is not None and config.backend is not None else backend
        )
        preference = resolve_backend_preference(
            explicit=explicit,
            env=None if explicit is not None else os.environ.get("NOTEBOOKLM_BACKEND"),
        )

    if config is not None:
        if config.backend is not None and config.backend.kind != preference.preferred:
            raise ValueError("The frozen backend preference does not match config.backend.kind")
        resolved = _resolved_config(config, preference)
        return NormalizedClientOptions(resolved, preference, (), (), True)

    normalized_upload = _timeout_options(upload_timeout) if upload_timeout is not None else None
    # Preserve the exact private sentinel identity on the compatibility path;
    # typed ``ClientConfig`` uses the public ``AUTO`` marker instead.
    normalized_chat = chat_timeout
    normalized_research = import_research_timeout
    upload_count = (
        DEFAULT_MAX_CONCURRENT_UPLOADS if max_concurrent_uploads is None else max_concurrent_uploads
    )
    runtime = _legacy_instance(
        RuntimeOptions,
        max_concurrent_rpcs=max_concurrent_rpcs,
        operation_timeout=None,
    )
    retry = _legacy_instance(
        RetryOptions,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
    )
    transfers = _legacy_instance(
        TransferOptions,
        max_concurrent_uploads=upload_count,
        start_timeout=normalized_upload,
        finalize_timeout=normalized_upload,
        drive_timeout=normalized_upload if preference.preferred == "android" else None,
    )
    features = _legacy_instance(
        FeatureOptions,
        chat_timeout=normalized_chat,
        chat_response_max_bytes=chat_response_max_bytes,
        import_research_timeout=normalized_research,
    )
    ignored: list[str] = []
    if preference.preferred == "web":
        effective_limits = limits
        if effective_limits is None:
            effective_limits = ConnectionLimits()
        selected_backend: WebBackendConfig | AndroidBackendConfig = _legacy_instance(
            WebBackendConfig,
            kind="web",
            transport=_legacy_instance(
                WebTransportOptions,
                read_timeout=timeout,
                write_timeout=timeout,
                pool_timeout=timeout,
                limits=effective_limits,
            ),
            session=_legacy_instance(
                WebSessionOptions,
                keepalive_interval=keepalive,
                keepalive_min_interval=keepalive_min_interval,
            ),
            hooks=(
                _legacy_instance(
                    WebSessionHooks,
                    cookie_saver=cookie_saver,
                    cookie_rotator=cookie_rotator,
                )
                if cookie_saver is not None or cookie_rotator is not None
                else None
            ),
        )
    else:
        selected_backend = _legacy_instance(
            AndroidBackendConfig,
            kind="android",
            rpc_timeout=timeout,
        )
        if keepalive is not None:
            ignored.append("keepalive")
        if keepalive_min_interval != DEFAULT_KEEPALIVE_MIN_INTERVAL:
            ignored.append("keepalive_min_interval")
        if cookie_saver is not None:
            ignored.append("cookie_saver")
        if cookie_rotator is not None:
            ignored.append("cookie_rotator")
        if limits is not None:
            ignored.append("limits")
    resolved = _legacy_instance(
        ClientConfig,
        backend=selected_backend,
        runtime=runtime,
        retry=retry,
        transfers=transfers,
        features=features,
        on_rpc_event=on_rpc_event,
    )
    return NormalizedClientOptions(
        resolved,
        preference,
        legacy_arguments,
        tuple(sorted(ignored)),
        False,
    )


__all__ = [
    "BackendPreference",
    "NormalizedClientOptions",
    "client_construction_context",
    "consume_storage_construction_preference",
    "legacy_client_option_names",
    "normalize_legacy_client_options",
    "resolve_backend_preference",
    "storage_construction_allocation",
]
