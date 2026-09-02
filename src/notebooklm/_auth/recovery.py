"""Client-neutral authentication recovery adapters."""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import httpx

from . import single_flight as _single_flight
from .cookie_types import CookieJar
from .paths import canonical_storage_key

if TYPE_CHECKING:
    from .cookies import _LoadedCookiePair
    from .extraction import _LoginRedirectError
    from .profile_migration import _LoadedProfilePair
    from .storage import CookieSnapshot

logger = logging.getLogger("notebooklm.auth")


@dataclass(frozen=True, slots=True, repr=False)
class ColdRecoveryResult:
    """Final shared jar and the baseline preceding validation mutations."""

    cookie_jar: httpx.Cookies = field(repr=False)
    snapshot: CookieSnapshot = field(repr=False)
    baseline: CookieJar = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.cookie_jar, httpx.Cookies) or not isinstance(
            self.baseline, CookieJar
        ):
            raise TypeError("cold recovery result fields are invalid")
        object.__setattr__(self, "baseline", CookieJar(tuple(self.baseline)))


@dataclass(frozen=True, slots=True, repr=False)
class _ColdRecoveryExhaustion:
    """Transport an expected redirect across a single-flight task boundary."""

    error: _LoginRedirectError = field(repr=False)


class ColdRecoveryState:
    """Own loop-local cold locks and success generations."""

    _process_default_owner: ClassVar[ColdRecoveryState]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks_by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, weakref.WeakValueDictionary[Path, asyncio.Lock]
        ] = weakref.WeakKeyDictionary()
        self._success_generations: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[Path, int]
        ] = weakref.WeakKeyDictionary()

    @classmethod
    def process_default(cls) -> ColdRecoveryState:
        return cls._process_default_owner

    def path_lock(self, path: Path) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._lock:
            per_loop = self._locks_by_loop.get(loop)
            if per_loop is None:
                per_loop = weakref.WeakValueDictionary()
                self._locks_by_loop[loop] = per_loop
            lock = per_loop.get(path)
            if lock is None:
                lock = asyncio.Lock()
                per_loop[path] = lock
            return lock

    def success_generation(self, path: Path) -> int:
        loop = asyncio.get_running_loop()
        with self._lock:
            return self._success_generations.get(loop, {}).get(path, 0)

    def note_success(self, path: Path) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            generations = self._success_generations.setdefault(loop, {})
            generations[path] = generations.get(path, 0) + 1

    def _reset_for_tests(self) -> None:
        with self._lock:
            if any(
                lock.locked() for locks in self._locks_by_loop.values() for lock in locks.values()
            ):
                raise RuntimeError("cannot reset ColdRecoveryState with locked paths")
            self._locks_by_loop.clear()
            self._success_generations.clear()


ColdRecoveryState._process_default_owner = ColdRecoveryState()


class ColdRecoveryCoordinator:
    """Run one explicit L2.5, L3, and L4 recovery operation."""

    def __init__(
        self,
        *,
        state: ColdRecoveryState,
        single_flight: _single_flight.SingleFlight,
        should_try_refresh: Callable[[Exception, bool], bool],
        resolve_refresh_path: Callable[[ValueError], Path],
        run_refresh_attempt: Callable[
            [Path], Awaitable[tuple[str, str, CookieSnapshot, CookieJar | None]]
        ],
        load_cookie_pair: Callable[[Path], _LoadedCookiePair],
        run_headless_attempt: Callable[[Path | None, bool], Awaitable[_LoadedCookiePair | None]],
        run_master_token_attempt: Callable[[Path | None], Awaitable[_LoadedCookiePair | None]],
        validate_recovered: Callable[[httpx.Cookies], Awaitable[None]],
        fetch_recovered: Callable[[httpx.Cookies], Awaitable[tuple[str, str]]],
        replace_cookie_jar: Callable[[httpx.Cookies, httpx.Cookies], None],
        snapshot_cookie_jar: Callable[[httpx.Cookies], CookieSnapshot],
        clone_cookie_jar: Callable[[httpx.Cookies], httpx.Cookies],
    ) -> None:
        self._claim_lock = threading.Lock()
        self._state = state
        self._single_flight = single_flight
        self._should_try_refresh = should_try_refresh
        self._resolve_refresh_path = resolve_refresh_path
        self._run_refresh_attempt = run_refresh_attempt
        self._load_cookie_pair = load_cookie_pair
        self._run_headless_attempt = run_headless_attempt
        self._run_master_token_attempt = run_master_token_attempt
        self._validate_recovered = validate_recovered
        self._fetch_recovered = fetch_recovered
        self._replace_cookie_jar = replace_cookie_jar
        self._snapshot_cookie_jar = snapshot_cookie_jar
        self._clone_cookie_jar = clone_cookie_jar
        self._used = False

    async def recover(
        self,
        *,
        initial_error: ValueError,
        cookie_jar: httpx.Cookies,
        storage_path: Path | None,
        env_auth: bool,
        allow_headless: bool,
        baseline: CookieJar | None,
    ) -> tuple[str, str, bool, CookieSnapshot | None, CookieJar | None]:
        with self._claim_lock:
            if self._used:
                raise RuntimeError("ColdRecoveryCoordinator.recover() is one-shot")
            self._used = True
        try:
            refresh_error: Exception | None = None
            if self._should_try_refresh(initial_error, env_auth):
                refresh_path = self._resolve_refresh_path(initial_error)
                try:
                    (
                        csrf,
                        session_id,
                        snapshot,
                        replacement_baseline,
                    ) = await self._run_refresh_attempt(refresh_path)
                except (RuntimeError, OSError, ValueError) as exc:
                    refresh_error = exc
                else:
                    return csrf, session_id, True, snapshot, replacement_baseline

            from .extraction import _LoginRedirectError

            if isinstance(initial_error, _LoginRedirectError) and storage_path is not None:
                try:
                    recovery = await self._coalesce_cold(
                        state=self._state,
                        single_flight=self._single_flight,
                        storage_path=storage_path,
                        allow_headless=allow_headless,
                        load_cookie_pair=self._load_cookie_pair,
                        run_headless_attempt=self._run_headless_attempt,
                        run_master_token_attempt=self._run_master_token_attempt,
                        validate_recovered=self._validate_recovered,
                        snapshot_cookie_jar=self._snapshot_cookie_jar,
                        clone_cookie_jar=self._clone_cookie_jar,
                        raise_on_exhaustion=False,
                        initial_error=initial_error,
                    )
                    if recovery is not None:
                        self._replace_cookie_jar(cookie_jar, recovery.cookie_jar)
                        csrf, session_id = await self._fetch_recovered(cookie_jar)
                except _LoginRedirectError:
                    pass
                else:
                    if recovery is not None:
                        return csrf, session_id, True, recovery.snapshot, recovery.baseline
            if refresh_error is not None:
                raise refresh_error
            raise
        finally:
            del self._should_try_refresh
            del self._resolve_refresh_path
            del self._run_refresh_attempt
            del self._load_cookie_pair
            del self._run_headless_attempt
            del self._run_master_token_attempt
            del self._validate_recovered
            del self._fetch_recovered
            del self._replace_cookie_jar
            del self._snapshot_cookie_jar
            del self._clone_cookie_jar

    @staticmethod
    async def _drive_cold(
        *,
        state: ColdRecoveryState,
        storage_path: Path,
        allow_headless: bool,
        load_cookie_pair: Callable[[Path], _LoadedCookiePair],
        run_headless_attempt: Callable[[Path | None, bool], Awaitable[_LoadedCookiePair | None]],
        run_master_token_attempt: Callable[[Path | None], Awaitable[_LoadedCookiePair | None]],
        validate_recovered: Callable[[httpx.Cookies], Awaitable[None]],
        snapshot_cookie_jar: Callable[[httpx.Cookies], CookieSnapshot],
        initial_error: _LoginRedirectError,
    ) -> ColdRecoveryResult:
        from .extraction import _LoginRedirectError

        async with state.path_lock(storage_path):
            initial = await asyncio.to_thread(load_cookie_pair, storage_path)
            working_jar = initial.live
            baseline = initial.baseline
            snapshot = snapshot_cookie_jar(working_jar)
            last_redirect = initial_error
            if state.success_generation(storage_path) > 0:
                try:
                    await validate_recovered(working_jar)
                except _LoginRedirectError as redirect_error:
                    last_redirect = redirect_error
                else:
                    return ColdRecoveryResult(working_jar, snapshot, baseline)

            replacement = await run_headless_attempt(storage_path, allow_headless)
            if replacement is not None:
                working_jar = replacement.live
                baseline = replacement.baseline
                snapshot = snapshot_cookie_jar(working_jar)
                try:
                    await validate_recovered(working_jar)
                except _LoginRedirectError as redirect_error:
                    last_redirect = redirect_error
                else:
                    state.note_success(storage_path)
                    return ColdRecoveryResult(working_jar, snapshot, baseline)

            replacement = await run_master_token_attempt(storage_path)
            if replacement is not None:
                working_jar = replacement.live
                baseline = replacement.baseline
                snapshot = snapshot_cookie_jar(working_jar)
                try:
                    await validate_recovered(working_jar)
                except _LoginRedirectError as redirect_error:
                    last_redirect = redirect_error
                else:
                    state.note_success(storage_path)
                    return ColdRecoveryResult(working_jar, snapshot, baseline)
            raise last_redirect

    @staticmethod
    async def _coalesce_cold(
        *,
        state: ColdRecoveryState,
        single_flight: _single_flight.SingleFlight,
        storage_path: Path,
        allow_headless: bool,
        load_cookie_pair: Callable[[Path], _LoadedCookiePair],
        run_headless_attempt: Callable[[Path | None, bool], Awaitable[_LoadedCookiePair | None]],
        run_master_token_attempt: Callable[[Path | None], Awaitable[_LoadedCookiePair | None]],
        validate_recovered: Callable[[httpx.Cookies], Awaitable[None]],
        snapshot_cookie_jar: Callable[[httpx.Cookies], CookieSnapshot],
        clone_cookie_jar: Callable[[httpx.Cookies], httpx.Cookies],
        raise_on_exhaustion: bool,
        initial_error: _LoginRedirectError,
    ) -> ColdRecoveryResult | None:
        from .extraction import _LoginRedirectError

        canonical_path = canonical_storage_key(storage_path)
        assert canonical_path is not None
        flight_key = (str(canonical_path), ("cold", allow_headless))

        async def _factory() -> ColdRecoveryResult | _ColdRecoveryExhaustion:
            try:
                return await ColdRecoveryCoordinator._drive_cold(
                    state=state,
                    storage_path=canonical_path,
                    allow_headless=allow_headless,
                    load_cookie_pair=load_cookie_pair,
                    run_headless_attempt=run_headless_attempt,
                    run_master_token_attempt=run_master_token_attempt,
                    validate_recovered=validate_recovered,
                    snapshot_cookie_jar=snapshot_cookie_jar,
                    initial_error=initial_error,
                )
            except _LoginRedirectError as error:
                traceback = error.__traceback__
                if traceback is not None:
                    error.__traceback__ = traceback.tb_next
                return _ColdRecoveryExhaustion(error)

        _is_leader, flight = single_flight.claim(flight_key, _factory)
        shared = await single_flight.await_flight(flight)
        if isinstance(shared, _ColdRecoveryExhaustion):
            if raise_on_exhaustion:
                raise shared.error
            return None
        return ColdRecoveryResult(
            clone_cookie_jar(shared.cookie_jar),
            dict(shared.snapshot),
            shared.baseline,
        )


async def _run_cold_recovery(
    *,
    storage_path: Path,
    allow_headless: bool,
    validate: Callable[[httpx.Cookies], Awaitable[None]],
    initial_error: _LoginRedirectError,
) -> ColdRecoveryResult:
    from .cookies import _build_cookie_pair_from_storage
    from .storage import snapshot_cookie_jar

    async def run_headless(path: Path | None, headless: bool) -> _LoadedCookiePair | None:
        return await _try_headless_reauth_result(
            storage_path=path,
            allow_headless=headless,
        )

    async def run_master_token(path: Path | None) -> _LoadedCookiePair | None:
        return await _try_master_token_reauth_result(storage_path=path)

    return await ColdRecoveryCoordinator._drive_cold(
        state=ColdRecoveryState.process_default(),
        storage_path=storage_path,
        allow_headless=allow_headless,
        load_cookie_pair=_build_cookie_pair_from_storage,
        run_headless_attempt=run_headless,
        run_master_token_attempt=run_master_token,
        validate_recovered=validate,
        snapshot_cookie_jar=snapshot_cookie_jar,
        initial_error=initial_error,
    )


async def coalesced_cold_recovery(
    *,
    storage_path: Path,
    allow_headless: bool,
    validate: Callable[[httpx.Cookies], Awaitable[None]],
    initial_error: _LoginRedirectError,
) -> ColdRecoveryResult:
    from .cookies import _build_cookie_pair_from_storage, _clone_cookie_jar
    from .storage import snapshot_cookie_jar

    async def run_headless(path: Path | None, headless: bool) -> _LoadedCookiePair | None:
        return await _try_headless_reauth_result(
            storage_path=path,
            allow_headless=headless,
        )

    async def run_master_token(path: Path | None) -> _LoadedCookiePair | None:
        return await _try_master_token_reauth_result(storage_path=path)

    return cast(
        ColdRecoveryResult,
        await ColdRecoveryCoordinator._coalesce_cold(
            state=ColdRecoveryState.process_default(),
            single_flight=_single_flight.SingleFlight.process_default(),
            storage_path=storage_path,
            allow_headless=allow_headless,
            load_cookie_pair=_build_cookie_pair_from_storage,
            run_headless_attempt=run_headless,
            run_master_token_attempt=run_master_token,
            validate_recovered=validate,
            snapshot_cookie_jar=snapshot_cookie_jar,
            clone_cookie_jar=_clone_cookie_jar,
            raise_on_exhaustion=True,
            initial_error=initial_error,
        ),
    )


async def try_storage_cookie_reload(
    *,
    storage_path: Path | None,
    cookie_jar: httpx.Cookies,
    rejected_cookie_jar: CookieJar | None = None,
    force_disk_read: bool = False,
    preserve_auth_material_change: bool = True,
    load_profile_pair: Callable[[Path], Awaitable[_LoadedProfilePair]] | None = None,
    install_profile: Callable[
        [httpx.Cookies, httpx.Cookies, CookieJar, int, str | None],
        Awaitable[bool | None],
    ]
    | None = None,
    adopt_baseline: Callable[[Path, CookieJar], Awaitable[None]] | None = None,
) -> bool:
    """Reload a file-backed session into a rejected live jar for one retry.

    This is the cheap, default-on bridge between a long-lived client's stale
    in-memory state and a profile that another process may already have
    refreshed. The loader is deliberately pure and name-only: it performs one
    disk read with no browser, subprocess, RotateCookies POST, or write. A jar
    changed after the rejected request or during the read is preserved and
    reported as retryable instead of being overwritten by the disk sample.
    ``force_disk_read`` still samples storage after a post-request change, but
    remembers that the current live state has not yet been tried. Ambient-only
    changes may be superseded by the sampled profile, while a live auth-material
    rotation is preserved against lagging disk. The caller disables that
    preservation only for the final bounded disk candidate. Cookies and the
    account route come from one raw profile generation and are installed together
    before an optional callback adopts the paired baseline for following saves.
    """
    from .cookies import _replace_cookie_jar
    from .profile_migration import _load_profile_pair_pure

    live_before = CookieJar.from_httpx(cookie_jar)
    live_changed_since_rejection = (
        rejected_cookie_jar is not None and live_before != rejected_cookie_jar
    )
    if live_changed_since_rejection and not force_disk_read:
        logger.info("Stored-cookie reload left a post-request jar change in place.")
        return True
    if storage_path is None:
        return live_changed_since_rejection

    try:
        if load_profile_pair is None:
            fresh_profile = await asyncio.to_thread(
                _load_profile_pair_pure,
                storage_path,
                require_routable=False,
            )
        else:
            fresh_profile = await load_profile_pair(storage_path)
    except (OSError, UnicodeError, TypeError, ValueError, OverflowError) as exc:
        logger.debug(
            "Stored-cookie reload skipped for %s (%s).",
            storage_path,
            type(exc).__name__,
        )
        # A forced sample is best-effort. If it fails, the post-request live
        # state is still untried and remains the only safe local retry.
        return live_changed_since_rejection

    fresh = fresh_profile.cookies
    account = fresh_profile.account
    authuser = 0 if account is None else account.authuser
    account_email = None if account is None else account.email
    live_after = CookieJar.from_httpx(cookie_jar)
    if live_after != live_before:
        logger.info(
            "Stored-cookie reload left a concurrently refreshed live jar in place for %s.",
            storage_path,
        )
        return True

    fresh_state = {cookie.key: cookie for cookie in CookieJar.from_httpx(fresh.live)}
    live_state = {cookie.key: cookie for cookie in live_after}
    if (
        fresh_state != live_state
        and live_changed_since_rejection
        and preserve_auth_material_change
        and _auth_material_changed(
            rejected=rejected_cookie_jar,
            live=live_after,
        )
    ):
        logger.info(
            "Stored-cookie reload sampled a different profile but retained an untried "
            "auth-material live-jar rotation for %s.",
            storage_path,
        )
        return True

    async def install_selected_profile() -> bool | None:
        if install_profile is None:
            _replace_cookie_jar(cookie_jar, fresh.live)
            return False
        return await install_profile(
            cookie_jar,
            fresh.live,
            live_after,
            authuser,
            account_email,
        )

    if fresh_state == live_state:
        route_changed = await install_selected_profile()
        if route_changed is None:
            logger.info(
                "Stored-cookie reload left a live jar changed while installing the "
                "sampled profile in place for %s.",
                storage_path,
            )
            return True
        if adopt_baseline is not None:
            await _try_adopt_storage_baseline(
                storage_path=storage_path,
                baseline=fresh.baseline,
                adopt_baseline=adopt_baseline,
            )
        if CookieJar.from_httpx(cookie_jar) != live_after:
            logger.info(
                "Stored-cookie reload left a live jar changed during baseline adoption "
                "in place for %s.",
                storage_path,
            )
            return True
        if live_changed_since_rejection or route_changed:
            logger.info(
                "Stored-cookie reload retained an untried live state or installed an "
                "updated account route for %s.",
                storage_path,
            )
            return True
        logger.debug("Stored-cookie reload skipped for %s: profile is unchanged.", storage_path)
        return False

    installed = await install_selected_profile()
    if installed is None:
        logger.info(
            "Stored-cookie reload left a live jar changed while installing the sampled "
            "profile in place for %s.",
            storage_path,
        )
        return True
    if adopt_baseline is not None:
        await _try_adopt_storage_baseline(
            storage_path=storage_path,
            baseline=fresh.baseline,
            adopt_baseline=adopt_baseline,
        )
    logger.info("Reloaded updated cookies from %s for authentication retry.", storage_path)
    return True


def _auth_material_changed(*, rejected: CookieJar | None, live: CookieJar) -> bool:
    """Return whether rejected→live changed authentication-bearing cookies."""
    if rejected is None:
        return False
    return rejected._auth_material_state() != live._auth_material_state()


async def _try_adopt_storage_baseline(
    *,
    storage_path: Path,
    baseline: CookieJar,
    adopt_baseline: Callable[[Path, CookieJar], Awaitable[None]],
) -> None:
    """Best-effort persistence adoption after the live-jar decision."""
    try:
        await adopt_baseline(storage_path, baseline)
    except (OSError, UnicodeError, TypeError, ValueError, OverflowError) as exc:
        logger.debug(
            "Stored-cookie baseline adoption skipped for %s (%s).",
            storage_path,
            type(exc).__name__,
        )


async def _try_headless_reauth_result(
    *,
    storage_path: Path | None,
    allow_headless: bool,
) -> _LoadedCookiePair | None:
    """Drive opt-in browser recovery and return its exact paired reload."""
    if storage_path is None:
        logger.debug("Headless re-auth skipped: auth has no writable storage path.")
        return None

    from .cookies import _build_cookie_pair_from_storage
    from .recovery_rungs import installed_headless_rung

    rung = installed_headless_rung()
    if rung is None:
        logger.debug("Headless re-auth skipped: no recovery rung is installed.")
        return None
    outcome = await asyncio.to_thread(
        rung,
        storage_path=storage_path,
        allow_headless=allow_headless,
    )
    if not outcome.succeeded:
        logger.debug(
            "Headless re-auth did not succeed (%s): %s",
            outcome.status.value,
            outcome.reason,
        )
        return None
    try:
        fresh = await asyncio.to_thread(_build_cookie_pair_from_storage, storage_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Headless re-auth wrote storage but its cookies failed to load (%s).",
            type(exc).__name__,
        )
        return None
    logger.info("Headless re-auth succeeded; reloaded re-minted cookies for retry.")
    return fresh


async def try_headless_reauth(
    *,
    storage_path: Path | None,
    cookie_jar: httpx.Cookies,
    allow_headless: bool,
) -> bool:
    """Drive opt-in browser recovery and reload the persisted cookie jar."""
    fresh = await _try_headless_reauth_result(
        storage_path=storage_path,
        allow_headless=allow_headless,
    )
    if fresh is None:
        return False

    from .cookies import _replace_cookie_jar

    _replace_cookie_jar(cookie_jar, fresh.live)
    return True


async def _run_master_token_reauth(*, storage_path: Path) -> _LoadedCookiePair | None:
    """Mint, persist, and reload one master-token session for shared callers.

    Delegates the read -> mint -> persist sequence to the shared kernel
    (:func:`notebooklm._auth.master_token.remint_from_stored_token`, #2103
    PR-2 D1) rather than assembling it here — this rung previously duplicated
    the same sequence the CLI's operator-refresh path also assembled
    independently. This wrapper keeps its OWN existing reload afterward
    (:func:`notebooklm._auth.cookies.build_httpx_cookies_from_storage`, with
    its inline-PSIDTS-recovery semantics) rather than trusting the kernel's
    internal (strict, side-effect-free) reload — L4's reload behavior is
    unchanged from before this PR (#2103 PR-2 F11)."""
    from .cookies import _build_cookie_pair_from_storage
    from .master_token import MasterTokenError, remint_from_stored_token

    try:
        await remint_from_stored_token(storage_path)
    except MasterTokenError as exc:
        logger.warning("Master-token re-mint failed (%s); authentication error stands.", exc)
        return None

    try:
        fresh = await asyncio.to_thread(_build_cookie_pair_from_storage, storage_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Master-token re-mint could not persist/reload cookies (%s); "
            "authentication error stands.",
            type(exc).__name__,
        )
        return None
    return fresh


async def _try_master_token_reauth_result(*, storage_path: Path | None) -> _LoadedCookiePair | None:
    """Share one L4 re-mint and return an isolated exact paired reload."""
    if storage_path is None:
        return None

    canonical_path = canonical_storage_key(storage_path)
    assert canonical_path is not None
    from ..paths import master_token_path_for

    master_token_path = master_token_path_for(canonical_path)
    if not master_token_path.exists():
        return None

    flight_key = (str(canonical_path), "master-token")

    def _factory() -> Coroutine[Any, Any, _LoadedCookiePair | None]:
        return _run_master_token_reauth(storage_path=canonical_path)

    _is_leader, flight = _single_flight.claim(flight_key, _factory)
    fresh = await _single_flight.await_flight(flight)
    if fresh is None:
        return None

    from .cookies import _clone_cookie_jar, _LoadedCookiePair

    logger.info("Master-token re-mint succeeded; reloaded fresh cookies for retry.")
    return _LoadedCookiePair(_clone_cookie_jar(fresh.live), fresh.baseline)


async def try_master_token_reauth(*, storage_path: Path | None, cookie_jar: httpx.Cookies) -> bool:
    """Share one L4 re-mint across overlapping cold and live callers, any loop."""
    fresh = await _try_master_token_reauth_result(storage_path=storage_path)
    if fresh is None:
        return False

    from .cookies import _replace_cookie_jar

    # Repopulate this caller's jar from a COPY of the shared result (CodeRabbit
    # #1): the single-flight jar is handed to every follower on every loop, so
    # cloning before we read it keeps concurrent followers isolated from one
    # another's jar mutation.
    _replace_cookie_jar(cookie_jar, fresh.live)
    return True
