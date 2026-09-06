"""Import-light contracts shared by the public client and Web lifecycle."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

import httpx

from ._runtime.lifecycle import LoopParticipant, TransportLifecycle
from ._types.common import CookieRotator, CookieSaver, SaveCookiesToStorage

if TYPE_CHECKING:
    from ._android.auth import MasterTokenReader, OAuthMinter
    from ._android.raw import AndroidRawAPI
    from ._android.runtime import AndroidRuntime
    from ._artifacts import ArtifactsAPI
    from ._chat import ChatAPI
    from ._collections import CollectionsAPI
    from ._labels import LabelsAPI
    from ._mind_maps_api import MindMapsAPI
    from ._notebooks import NotebooksAPI
    from ._notes import NotesAPI
    from ._research import BaseResearchAPI
    from ._runtime.init import SharedRuntime, SharedRuntimeConfig
    from ._settings import SettingsAPI
    from ._sharing import SharingAPI
    from ._sources import SourcesAPI
    from ._web.raw import WebRawAPI
    from ._web.transport.composed import ClientComposed
    from ._web.transport.init import WebRuntime
    from ._web.transport.seams import ClientSeams
    from .auth import AuthTokens
    from .options import (
        AndroidBackendConfig,
        FeatureOptions,
        RetryOptions,
        TransferOptions,
        WebBackendConfig,
    )


BackendName = Literal["web", "android"]

_NAMESPACE_NAMES = (
    "notebooks",
    "sources",
    "artifacts",
    "chat",
    "research",
    "notes",
    "mind_maps",
    "settings",
    "sharing",
    "labels",
    "collections",
)


def installed_backend_map(backend: BackendName) -> Mapping[str, BackendName]:
    """Return an immutable, explicit namespace-to-backend report."""

    return MappingProxyType(dict.fromkeys(_NAMESPACE_NAMES, backend))


@dataclass(frozen=True)
class FeatureNamespaces:
    """Complete neutral namespace graph returned by a backend builder."""

    notebooks: NotebooksAPI
    sources: SourcesAPI
    artifacts: ArtifactsAPI
    chat: ChatAPI
    research: BaseResearchAPI
    notes: NotesAPI
    mind_maps: MindMapsAPI
    settings: SettingsAPI
    sharing: SharingAPI
    labels: LabelsAPI
    collections: CollectionsAPI


@dataclass(frozen=True)
class WebAssembly:
    """Complete Web graph, including its typed raw and runtime owners."""

    backend: Literal["web"]
    namespaces: FeatureNamespaces
    raw: WebRawAPI
    runtime: WebRuntime
    shared: SharedRuntime
    transports: tuple[TransportLifecycle, ...]
    loop_participants: tuple[LoopParticipant, ...]
    backends: Mapping[str, BackendName]
    seams: ClientSeams


@dataclass(frozen=True)
class AndroidAssembly:
    """Complete Android graph, including its typed raw and runtime owners."""

    backend: Literal["android"]
    namespaces: FeatureNamespaces
    raw: AndroidRawAPI
    runtime: AndroidRuntime
    shared: SharedRuntime
    transports: tuple[TransportLifecycle, ...]
    loop_participants: tuple[LoopParticipant, ...]
    backends: Mapping[str, BackendName]


BackendAssembly: TypeAlias = WebAssembly | AndroidAssembly


@dataclass(frozen=True)
class WebAssemblyConfig:
    """Owner-grouped settings consumed by the Web builder."""

    backend: WebBackendConfig
    retry: RetryOptions
    transfers: TransferOptions
    features: FeatureOptions
    shared_config: SharedRuntimeConfig


@dataclass(frozen=True)
class WebCredentials:
    """Credential inputs whose interpretation belongs to the Web builder."""

    auth: AuthTokens
    storage_path: Path | None
    keepalive_storage_path: Path | None


@dataclass(frozen=True)
class WebDependencies:
    """Private injectable collaborators for Web construction."""

    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None
    use_default_refresh_callback: bool
    refresh_retry_delay: float
    connect_timeout: float
    async_client_factory: Callable[..., httpx.AsyncClient] | None
    decode_response: Callable[..., Any] | None
    sleep: Callable[[float], Awaitable[Any]] | None
    is_auth_error: Callable[[Exception], bool] | None
    # Compatibility projection; TransferOptions remains the upload-phase owner.
    legacy_upload_timeout: httpx.Timeout | None
    seams: ClientSeams | None = None
    composed: ClientComposed | None = None

    def __post_init__(self) -> None:
        _validate_refresh_retry_delay(self.refresh_retry_delay)


@dataclass(frozen=True)
class AndroidAssemblyConfig:
    """Owner-grouped settings consumed by the Android builder."""

    backend: AndroidBackendConfig
    retry: RetryOptions
    transfers: TransferOptions
    features: FeatureOptions
    shared_config: SharedRuntimeConfig


@dataclass(frozen=True)
class AndroidCredentials:
    """Credential-source inputs owned by Android construction."""

    profile_path: Path | None


@dataclass(frozen=True)
class AndroidDependencies:
    """Private injectable collaborators for Android construction."""

    master_token_reader: MasterTokenReader | None
    oauth_minter: OAuthMinter | None
    sleep: Callable[[float], Awaitable[Any]] | None
    refresh_retry_delay: float

    def __post_init__(self) -> None:
        _validate_refresh_retry_delay(self.refresh_retry_delay)


def _validate_refresh_retry_delay(value: float) -> None:
    """Validate the private retry-delay seam at its dependency boundary."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"refresh_retry_delay must be finite and >= 0, got {value!r}")
    if value < 0:
        raise ValueError(f"refresh_retry_delay must be finite and >= 0, got {value!r}")


__all__ = [
    "AndroidAssembly",
    "AndroidAssemblyConfig",
    "AndroidCredentials",
    "AndroidDependencies",
    "BackendAssembly",
    "BackendName",
    "CookieRotator",
    "CookieSaver",
    "FeatureNamespaces",
    "SaveCookiesToStorage",
    "WebAssembly",
    "WebAssemblyConfig",
    "WebCredentials",
    "WebDependencies",
    "installed_backend_map",
]
