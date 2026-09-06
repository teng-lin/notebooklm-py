"""Import-light backend selector shared by production and the test factory."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from ._auth.tokens import FileLoadedAuth, LoadedAuth
from ._client_compat import (
    CompatibilityDependencies,
    CompatibilitySpec,
    LazyWebSidecar,
    WebSeamOverrides,
    build_compatibility_sidecar,
)
from ._client_contracts import (
    AndroidAssemblyConfig,
    AndroidCredentials,
    AndroidDependencies,
    BackendAssembly,
    BackendName,
    WebAssembly,
    WebAssemblyConfig,
    WebCredentials,
    WebDependencies,
)
from ._client_options import (
    BackendPreference,
    NormalizedClientOptions,
    resolve_backend_preference,
)
from ._runtime.config import (
    DEFAULT_CONNECT_TIMEOUT,
    validate_read_timeout_kwarg,
)
from ._runtime.error_injection import _refuse_synthetic_error_outside_test_context
from ._runtime.init import SharedRuntimeConfig, build_collaborators, validate_shared_runtime_config
from ._runtime.lifecycle import ClientLifecycle
from .auth import AuthTokens

if TYPE_CHECKING:
    from ._android.auth import MasterTokenReader, OAuthMinter
    from .client import NotebookLMClient

logger = logging.getLogger("notebooklm.backend")


class _UnsetType:
    """Sentinel for test-factory overrides whose ``None`` value is meaningful."""


_UNSET = _UnsetType()


def _install_client(
    client: NotebookLMClient,
    *,
    auth: AuthTokens,
    preference: BackendPreference,
    assembly: BackendAssembly,
    sidecar: LazyWebSidecar | None,
    android_seams: WebSeamOverrides | None,
) -> None:
    """Install one completed graph; this is the sole client mutation owner."""

    transports = assembly.transports
    loop_participants = assembly.loop_participants
    if sidecar is not None:
        transports = (*transports, sidecar)
        loop_participants = (*loop_participants, sidecar)
    lifecycle = ClientLifecycle(
        supervisor=assembly.shared.call_supervisor,
        transports=transports,
        loop_participants=loop_participants,
    )
    client._auth = auth
    client._account_email_cache = None
    client._account_email_cache_route = None
    client._backend_preference = preference
    client._collaborators = assembly.shared
    client._lifecycle = lifecycle
    client._backends = assembly.backends
    client._rpc_call_deprecation_warned = False
    client._raw = assembly.raw
    client.notebooks = assembly.namespaces.notebooks
    client.sources = assembly.namespaces.sources
    client.artifacts = assembly.namespaces.artifacts
    client.chat = assembly.namespaces.chat
    client.research = assembly.namespaces.research
    client.notes = assembly.namespaces.notes
    client.mind_maps = assembly.namespaces.mind_maps
    client.settings = assembly.namespaces.settings
    client.sharing = assembly.namespaces.sharing
    client.labels = assembly.namespaces.labels
    client.collections = assembly.namespaces.collections
    if isinstance(assembly, WebAssembly):
        client._web_runtime = assembly.runtime
        client._android_runtime = None
        client._web_sidecar = None
        client._seams = assembly.seams
    else:
        client._web_runtime = None
        client._android_runtime = assembly.runtime
        client._web_sidecar = sidecar
        client._seams = android_seams


def _finalize_loaded_client(
    client: NotebookLMClient,
    *,
    preference: BackendPreference,
    loaded_auth: LoadedAuth,
) -> None:
    """Install stored-auth provenance on the exact client returned by its class call."""

    client._backend_preference = preference
    if not isinstance(loaded_auth, FileLoadedAuth):
        return
    if hasattr(client, "_web_runtime") and client._web_runtime is not None:
        client._web_runtime.cookie_persistence.register_open_baseline(
            loaded_auth.store,
            loaded_auth.persistence_baseline,
        )


def _assemble_client(
    client: NotebookLMClient,
    *,
    auth: AuthTokens,
    options: NormalizedClientOptions,
    storage_path: Path | None = None,
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None | _UnsetType = _UNSET,
    refresh_retry_delay: float = 0.2,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    keepalive_storage_path: Path | None | _UnsetType = _UNSET,
    decode_response: Callable[..., Any] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    master_token_reader: MasterTokenReader | _UnsetType = _UNSET,
    oauth_minter: OAuthMinter | _UnsetType = _UNSET,
) -> None:
    """Normalize root-owned inputs, select one backend, and freeze its lifecycle."""

    preference = options.preference
    config = options.config
    selected_backend = config.backend
    if selected_backend is None:  # pragma: no cover - normalizer invariant
        raise AssertionError("normalized ClientConfig must select a backend")
    runtime_options = config.runtime
    retry_options = config.retry
    transfer_options = config.transfers
    feature_options = config.features
    if storage_path is not None:
        storage_path = Path(storage_path)
        if auth.storage_path != storage_path:
            auth = dataclasses.replace(auth, storage_path=storage_path)

    use_default_refresh_callback = isinstance(refresh_callback, _UnsetType)
    if use_default_refresh_callback:
        effective_refresh_callback = None
    else:
        effective_refresh_callback = cast(
            Callable[[int], Awaitable[AuthTokens]] | None,
            refresh_callback,
        )

    if isinstance(keepalive_storage_path, _UnsetType):
        derived_keepalive_path: Path | None = auth.storage_path
        if derived_keepalive_path is not None:
            derived_keepalive_path = Path(derived_keepalive_path).expanduser().resolve()
        keepalive_storage_path = derived_keepalive_path

    shared_config = SharedRuntimeConfig(
        max_concurrent_rpcs=runtime_options.max_concurrent_rpcs,
        operation_timeout=runtime_options.operation_timeout,
    )
    if preference.preferred == "web" and shared_config.max_concurrent_rpcs is not None:
        from .options import WebBackendConfig

        if not isinstance(selected_backend, WebBackendConfig):
            raise ValueError("Web preference requires WebBackendConfig")
        effective_limits = selected_backend.transport.limits
        if shared_config.max_concurrent_rpcs > effective_limits.max_connections:
            raise ValueError(
                "max_concurrent_rpcs must be <= limits.max_connections "
                f"(got max_concurrent_rpcs={shared_config.max_concurrent_rpcs}, "
                f"max_connections={effective_limits.max_connections}). "
                "A semaphore wider than the connection pool surfaces "
                "saturation as opaque httpx.PoolTimeout instead of clean back-pressure."
            )
    if (
        feature_options.chat_response_max_bytes is not None
        and feature_options.chat_response_max_bytes < 1
    ):
        raise ValueError(
            "chat_response_max_bytes must be >= 1 when supplied "
            f"(got {feature_options.chat_response_max_bytes!r})"
        )
    validate_read_timeout_kwarg(feature_options.chat_timeout, name="chat_timeout")
    validate_read_timeout_kwarg(
        feature_options.import_research_timeout,
        name="import_research_timeout",
    )

    shared_config = validate_shared_runtime_config(
        max_concurrent_rpcs=shared_config.max_concurrent_rpcs,
        operation_timeout=shared_config.operation_timeout,
    )
    _refuse_synthetic_error_outside_test_context()
    shared = build_collaborators(shared_config, on_rpc_event=config.on_rpc_event)

    if preference.preferred == "android":
        from .options import AndroidBackendConfig

        if not isinstance(selected_backend, AndroidBackendConfig):
            raise ValueError("Android preference requires AndroidBackendConfig")
        if options.ignored_web_arguments:
            logger.debug(
                "Android backend ignores Web-only options: %s",
                ", ".join(options.ignored_web_arguments),
            )

        from ._android.assembly import assemble_android_backend

        seam_overrides = WebSeamOverrides(
            decode_response=decode_response,
            sleep=sleep,
            is_auth_error=is_auth_error,
        )
        android_assembly = assemble_android_backend(
            shared=shared,
            config=AndroidAssemblyConfig(
                backend=selected_backend,
                retry=retry_options,
                transfers=transfer_options,
                features=feature_options,
                shared_config=shared_config,
            ),
            credentials=AndroidCredentials(
                profile_path=Path(auth.storage_path) if auth.storage_path is not None else None,
            ),
            deps=AndroidDependencies(
                master_token_reader=(
                    None if isinstance(master_token_reader, _UnsetType) else master_token_reader
                ),
                oauth_minter=None if isinstance(oauth_minter, _UnsetType) else oauth_minter,
                sleep=sleep,
                refresh_retry_delay=refresh_retry_delay,
            ),
        )
        sidecar_timeout = selected_backend.rpc_timeout if not options.typed_config else 30.0
        sidecar = build_compatibility_sidecar(
            shared,
            CompatibilitySpec(
                auth=auth,
                shared_config=shared_config,
                read_timeout=sidecar_timeout,
                write_timeout=sidecar_timeout,
                pool_timeout=sidecar_timeout,
                refresh_retry_delay=refresh_retry_delay,
                rate_limit_max_retries=retry_options.rate_limit_max_retries,
                server_error_max_retries=retry_options.server_error_max_retries,
                max_concurrent_uploads=transfer_options.max_concurrent_uploads,
            ),
            CompatibilityDependencies(
                seam_overrides=seam_overrides,
                refresh_callback=effective_refresh_callback,
                use_default_refresh_callback=use_default_refresh_callback,
                async_client_factory=async_client_factory,
            ),
        )
        _install_client(
            client,
            auth=auth,
            preference=preference,
            assembly=android_assembly,
            sidecar=sidecar,
            android_seams=seam_overrides,
        )
        return

    from ._web.assembly import assemble_web_backend
    from .options import WebBackendConfig

    if not isinstance(selected_backend, WebBackendConfig):
        raise ValueError("Web preference requires WebBackendConfig")

    web_assembly = assemble_web_backend(
        shared=shared,
        config=WebAssemblyConfig(
            backend=selected_backend,
            retry=retry_options,
            transfers=transfer_options,
            features=feature_options,
            shared_config=shared_config,
        ),
        credentials=WebCredentials(
            auth=auth,
            storage_path=storage_path,
            keepalive_storage_path=keepalive_storage_path,
        ),
        deps=WebDependencies(
            refresh_callback=effective_refresh_callback,
            use_default_refresh_callback=use_default_refresh_callback,
            refresh_retry_delay=refresh_retry_delay,
            connect_timeout=connect_timeout,
            async_client_factory=async_client_factory,
            decode_response=decode_response,
            sleep=sleep,
            is_auth_error=is_auth_error,
            legacy_upload_timeout=options.legacy_upload_timeout,
        ),
    )
    _install_client(
        client,
        auth=auth,
        preference=preference,
        assembly=web_assembly,
        sidecar=None,
        android_seams=None,
    )


__all__ = [
    "BackendName",
    "BackendPreference",
    "_assemble_client",
    "_finalize_loaded_client",
    "resolve_backend_preference",
]
