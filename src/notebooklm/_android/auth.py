"""Generation-aware Android bearer acquisition."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .._auth.master_token_types import MasterToken
from .._auth.mint_service import (
    MintedOAuthToken,
    MintService,
    OAuthClientSpec,
    OAuthMintError,
    _require_gpsoauth,
)
from .._auth.profile_store import ProfileStore
from .._loop_affinity import assert_bound_loop
from .._loop_bound import LoopBoundPrimitive
from ..exceptions import AuthError, ConfigurationError, MissingDependencyError
from .errors import sanitize_escaping_exception

_ANDROID_SCOPES = (
    "https://www.googleapis.com/auth/account_settings_mobile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "https://www.googleapis.com/auth/labs-tailwind",
    "https://www.googleapis.com/auth/notifications",
    "https://www.googleapis.com/auth/photos.image.readonly",
    "https://www.googleapis.com/auth/supportcontent",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

NOTEBOOKLM_OAUTH_SPEC = OAuthClientSpec(
    service="oauth2:" + " ".join(_ANDROID_SCOPES),
    app="com.google.android.apps.labs.language.tailwind",
    client_sig="a3382adf91991e6ef1e7e7de309c1febfedf3283",
)

_EXPIRY_MARGIN_CAP_SECONDS = 60.0
_EXPIRY_MARGIN_FRACTION = 0.1
_INACTIVE_MESSAGE = "Android authentication is not active for this client generation."
_MASTER_TOKEN_MESSAGE = (
    "Android authentication requires a valid master-token profile. "
    "Run `notebooklm login --master-token`."
)
_MINT_FAILURE_MESSAGE = "Android authentication could not mint an access token."
_ANDROID_EXTRA_MESSAGE = (
    "Android authentication needs optional dependencies. "
    "Install: pip install 'notebooklm-py[android]'"
)


class _ProfileReader(Protocol):
    def read_master_token(self) -> MasterToken | None: ...


class _OAuthMinter(Protocol):
    async def mint_oauth(
        self,
        master_token: MasterToken,
        spec: OAuthClientSpec,
    ) -> MintedOAuthToken: ...


class _NoMasterTokenProfile:
    """I/O-free reader for direct clients without a profile-backed path."""

    def read_master_token(self) -> None:
        return None


@dataclass(frozen=True)
class BearerCredential:
    """A short-lived bearer handle whose repr never exposes the credential."""

    token: str = field(repr=False)
    generation: int

    def __repr__(self) -> str:
        return f"BearerCredential(token=<redacted>, generation={self.generation})"


@dataclass(frozen=True)
class _MintResult:
    credential: BearerCredential = field(repr=False)
    cache_deadline: float | None


class BearerProvider(LoopBoundPrimitive):
    """Load a durable profile record and mint one shared short-lived bearer."""

    def __init__(
        self,
        profile_store: ProfileStore | _ProfileReader,
        mint_service: MintService | _OAuthMinter,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile_store = profile_store
        self._mint_service = mint_service
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._provider_epoch = 0
        self._active_session_epoch: int | None = None
        self._master_token: MasterToken | None = None
        self._lock: asyncio.Lock | None = None
        self._mint_task: asyncio.Task[_MintResult] | None = None
        self._mint_waiters = 0
        self._cached: BearerCredential | None = None
        self._cache_deadline: float | None = None
        self._bearer_generation = 0

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the loop assigned by the root lifecycle."""

        return self._bound_loop

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._lock = None

    def reset_after_open(self) -> None:
        """Discard loop-local state without resetting monotonic generations."""

        task = self._mint_task
        if task is not None:
            task.cancel()
            task.add_done_callback(self._consume_task_result)
        self._lock = None
        self._mint_task = None
        self._mint_waiters = 0
        self._active_session_epoch = None
        self._master_token = None
        self._cached = None
        self._cache_deadline = None

    @staticmethod
    def _consume_task_result(task: asyncio.Task[_MintResult]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            pass

    def _assert_loop(self) -> None:
        if self._bound_loop is None:
            raise RuntimeError("Android authentication was not bound by the client lifecycle.")
        assert_bound_loop(self._bound_loop)

    def _assert_active(self, expected_epoch: int) -> int:
        self._assert_loop()
        if self._active_session_epoch != expected_epoch or self._master_token is None:
            raise RuntimeError(_INACTIVE_MESSAGE)
        return self._provider_epoch

    def _get_lock(self) -> asyncio.Lock:
        lock = self._lock
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    async def activate(self, epoch: int) -> None:
        """Read and retain the typed durable credential without minting."""

        self._assert_loop()
        dependency_missing = False
        try:
            _require_gpsoauth()
        except MissingDependencyError:
            dependency_missing = True
        if dependency_missing:
            raise MissingDependencyError(_ANDROID_EXTRA_MESSAGE)
        self._provider_epoch += 1
        provider_epoch = self._provider_epoch
        self._active_session_epoch = epoch
        self._master_token = None
        self._cached = None
        self._cache_deadline = None
        try:
            record = await asyncio.to_thread(self._profile_store.read_master_token)
        except asyncio.CancelledError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            record = None

        if self._provider_epoch != provider_epoch or self._active_session_epoch != epoch:
            return
        if type(record) is not MasterToken:
            del record
            raise ConfigurationError(_MASTER_TOKEN_MESSAGE)
        self._master_token = record

    async def get(self, expected_epoch: int) -> BearerCredential:
        """Publish a bearer without retaining this secret owner in failures."""

        provider = self
        failure: BaseException | None = None
        result: BearerCredential | None = None
        try:
            result = await provider._get_impl(expected_epoch)
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, provider
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def _get_impl(self, expected_epoch: int) -> BearerCredential:
        """Return a cached bearer or join the current generation's mint wave."""

        provider_epoch = self._assert_active(expected_epoch)
        lock = self._get_lock()
        async with lock:
            self._assert_active(expected_epoch)
            cached = self._cached
            deadline = self._cache_deadline
            if cached is not None and deadline is not None and self._monotonic() < deadline:
                return cached
            self._cached = None
            self._cache_deadline = None

            task = self._mint_task
            if task is None:
                task = asyncio.create_task(
                    self._mint_once(provider_epoch),
                    name=f"notebooklm-android-bearer-{provider_epoch}",
                )
                task.add_done_callback(self._mint_done)
                self._mint_task = task
            self._mint_waiters += 1

        try:
            result = await asyncio.shield(task)
        except BaseException:
            self._settle_mint_waiter(task)
            raise

        try:
            await lock.acquire()
        except BaseException:
            self._settle_mint_waiter(task)
            del result
            raise
        try:
            if (
                self._provider_epoch != provider_epoch
                or self._active_session_epoch != expected_epoch
                or self._master_token is None
            ):
                del result
                raise RuntimeError(_INACTIVE_MESSAGE)
            if result.cache_deadline is not None and self._monotonic() < result.cache_deadline:
                self._cached = result.credential
                self._cache_deadline = result.cache_deadline
            return result.credential
        finally:
            self._settle_mint_waiter(task)
            lock.release()

    def _settle_mint_waiter(self, task: asyncio.Task[_MintResult]) -> None:
        if self._mint_task is task:
            self._mint_waiters -= 1
            if self._mint_waiters == 0 and task.done():
                self._mint_task = None

    def _mint_done(self, task: asyncio.Task[_MintResult]) -> None:
        self._consume_task_result(task)
        if self._mint_task is task and self._mint_waiters == 0:
            self._mint_task = None

    async def _mint_once(self, provider_epoch: int) -> _MintResult:
        record = self._master_token
        if record is None or self._provider_epoch != provider_epoch:
            raise RuntimeError(_INACTIVE_MESSAGE)

        failure: Exception | None = None
        minted: MintedOAuthToken | None = None
        try:
            minted = await self._mint_service.mint_oauth(record, NOTEBOOKLM_OAUTH_SPEC)
        except MissingDependencyError:
            failure = MissingDependencyError(_ANDROID_EXTRA_MESSAGE)
        except OAuthMintError:
            failure = AuthError(_MINT_FAILURE_MESSAGE)
        except asyncio.CancelledError:
            record = None
            raise
        except (KeyboardInterrupt, SystemExit):
            record = None
            raise
        except Exception:
            failure = AuthError(_MINT_FAILURE_MESSAGE)

        record = None
        if failure is not None:
            raise failure
        if not isinstance(minted, MintedOAuthToken) or not minted.token:
            raise AuthError(_MINT_FAILURE_MESSAGE)
        if self._provider_epoch != provider_epoch or self._active_session_epoch is None:
            del minted
            raise RuntimeError(_INACTIVE_MESSAGE)

        self._bearer_generation += 1
        credential = BearerCredential(
            token=minted.token,
            generation=self._bearer_generation,
        )
        cache_deadline = self._expiry_deadline(minted.expires_at)
        return _MintResult(credential=credential, cache_deadline=cache_deadline)

    def _expiry_deadline(self, expires_at: int | None) -> float | None:
        if type(expires_at) is not int:
            return None
        wall_now = self._wall_clock()
        monotonic_now = self._monotonic()
        remaining = float(expires_at) - wall_now
        if remaining <= 0.0:
            return None
        margin = min(_EXPIRY_MARGIN_CAP_SECONDS, remaining * _EXPIRY_MARGIN_FRACTION)
        deadline = monotonic_now + remaining - margin
        return deadline if deadline > monotonic_now else None

    def invalidate(self, generation: int) -> None:
        """Clear only the bearer generation used by a failed attempt."""

        cached = self._cached
        if cached is not None and cached.generation == generation:
            self._cached = None
            self._cache_deadline = None

    async def prepare_close(self) -> None:
        """Fence credentials synchronously, then cancel the retained mint task."""

        self._assert_loop()
        self._provider_epoch += 1
        self._active_session_epoch = None
        self._master_token = None
        self._cached = None
        self._cache_deadline = None
        task = self._mint_task
        self._mint_task = None
        self._mint_waiters = 0
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _make_bearer_provider(storage_path: Path | None) -> BearerProvider:
    """Assemble the concrete credential owners without reading the profile."""

    profile_reader = (
        ProfileStore(storage_path) if storage_path is not None else _NoMasterTokenProfile()
    )
    return BearerProvider(profile_reader, MintService())


__all__ = ["BearerCredential", "BearerProvider", "NOTEBOOKLM_OAUTH_SPEC"]
