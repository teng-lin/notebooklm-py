"""Cookie-generation port consumed by the legacy web client runtime.

The web backend receives this port rather than profile storage, interactive
authentication, or a mutable cookie jar.  A generation is an immutable,
atomic observation of every value needed to route one authenticated web
attempt.  Concrete acquisition/persistence remains in the auth/runtime layers.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ._auth.cookie_types import CookieJar
from ._source_upload_port import UploadLifecycleHooks

if TYPE_CHECKING:
    from ._auth.profile_store import ProfileStore
    from ._runtime.contracts import ChatLifecycleHooks
    from .auth import AuthTokens


@dataclass(frozen=True, slots=True)
class WebCookieGeneration:
    """One immutable cookie, token, and account-route generation.

    Cookie and token values are credential-equivalent and therefore excluded
    from ``repr``.  ``CookieJar`` is already immutable; copying it at the
    provider boundary also prevents a backend session from aliasing provider
    state.
    """

    csrf_token: str = field(repr=False)
    session_id: str = field(repr=False)
    authuser: int
    account_email: str | None
    cookies: CookieJar = field(default_factory=CookieJar, repr=False)
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.cookies, CookieJar):
            raise TypeError("cookies must be a CookieJar")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        # Defensive value copy even though CookieJar is immutable.  This pins
        # the provider contract to value ownership rather than caller identity.
        object.__setattr__(self, "cookies", CookieJar(tuple(self.cookies)))


@dataclass(frozen=True, slots=True)
class WebCookieSessionState:
    """Detached state returned by one private backend session."""

    cookies: CookieJar = field(repr=False)
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.cookies, CookieJar):
            raise TypeError("cookies must be a CookieJar")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        object.__setattr__(self, "cookies", CookieJar(tuple(self.cookies)))


class WebCookieSessionTransition(Protocol):
    """Quiescent private-session state plus its generation installer."""

    @property
    def state(self) -> WebCookieSessionState | None:
        """Return cookies after every admitted response has settled."""
        ...

    def install(self, generation: WebCookieGeneration) -> bool:
        """Install a provider generation before reopening request admission."""
        ...


class WebCookieSession(Protocol):
    """Backend-private session seeded only from provider generations."""

    @property
    def is_open(self) -> bool:
        """Return whether the private transport is open."""
        ...

    def assert_open(self) -> None:
        """Raise the legacy not-open error when the session is closed."""
        ...

    async def open(self, generation: WebCookieGeneration) -> None:
        """Clone one provider generation and open the private transport."""
        ...

    def detach(self) -> WebCookieSessionState | None:
        """Return a detached final state, or ``None`` before first install."""
        ...

    def generation_transition(self) -> AbstractAsyncContextManager[WebCookieSessionTransition]:
        """Close admission and expose one quiescent cookie-jar transaction."""
        ...

    async def close(self) -> None:
        """Close the private transport without persisting credentials."""
        ...


class WebCookieProvider(Protocol):
    """Acquire and maintain generations for one legacy web client.

    The protocol deliberately exposes whole transactions.  It does not expose
    profile paths, lock primitives, storage documents, browser drivers, or a
    mutable HTTP cookie container.
    """

    @property
    def auth(self) -> AuthTokens:
        """Return the preserved public/bootstrap ``AuthTokens`` identity."""
        ...

    async def generation(self) -> WebCookieGeneration:
        """Return one atomically captured immutable generation."""
        ...

    async def reconciled_generation(self) -> WebCookieGeneration:
        """Reconcile a matching backend response for a direct HTTP leg."""
        ...

    @property
    def is_open(self) -> bool:
        """Return whether the provider acquisition session is open."""
        ...

    async def open(self, *, uploader: UploadLifecycleHooks, chat: ChatLifecycleHooks) -> None:
        """Open provider-owned acquisition and persistence resources."""
        ...

    async def refresh(self, *, allow_headless: bool = False) -> AuthTokens:
        """Run the existing refresh policy and publish its new generation."""
        ...

    @property
    def has_refresh_callback(self) -> bool:
        """Return whether automatic refresh was configured."""
        ...

    async def await_refresh(self) -> None:
        """Join the configured refresh flight and publish its outcome."""
        ...

    def get_account_authuser(self) -> int:
        """Return the compatibility account index without network I/O."""
        ...

    async def get_account_email(self, *, live_fallback: bool = True) -> str | None:
        """Resolve the compatibility account identity."""
        ...

    def register_open_baseline(self, store: ProfileStore, baseline: CookieJar) -> None:
        """Install a load-time persistence baseline during construction."""
        ...

    async def reconcile(self) -> None:
        """Adopt a matching private-session result as one newer generation."""
        ...

    async def close(self, *, reconcile_backend: bool = True) -> None:
        """Release owned resources, optionally skipping a blocking reconciliation."""
        ...


__all__ = [
    "WebCookieGeneration",
    "WebCookieProvider",
    "WebCookieSession",
    "WebCookieSessionState",
    "WebCookieSessionTransition",
]
