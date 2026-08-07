"""Client-neutral authentication recovery adapters."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import single_flight as _single_flight
from .paths import canonical_storage_key

if TYPE_CHECKING:
    from .extraction import _LoginRedirectError
    from .storage import CookieSnapshot

logger = logging.getLogger("notebooklm.auth")


@dataclass(frozen=True)
class ColdRecoveryResult:
    """Final shared jar and the baseline preceding validation mutations."""

    cookie_jar: httpx.Cookies
    snapshot: CookieSnapshot


# Cross-loop coalescing of both the cold ladder and the L4 master-token re-mint
# now flows through ``notebooklm._auth.single_flight`` (c-PR2). The old per-loop
# in-flight task registries (``_COLD_INFLIGHT_BY_LOOP`` /
# ``_MASTER_INFLIGHT_BY_LOOP``) and the hand-rolled ``_await_shared_task``
# settle loop were deleted in the same PR.
#
# The two structures that remain here are CONSUMER-SIDE policy, deliberately NOT
# promoted to the cross-loop core (plan §c.1):
#   * ``_COLD_LOCKS_BY_LOOP`` — a per-loop asyncio.Lock serializing the ladder
#     across rung policies on one loop.
#   * ``_COLD_SUCCESS_GENERATIONS`` — the per-loop revalidate-on-bump epoch: a
#     fresh loop that already succeeded revalidates against the network before
#     re-running the full ladder. Promoting this to cross-loop would change the
#     fresh-loop-runs-full-ladder behavior, so it stays per-loop here.
_COLD_LOCKS_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]
] = weakref.WeakKeyDictionary()
_COLD_SUCCESS_GENERATIONS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, int]] = (
    weakref.WeakKeyDictionary()
)


def _cold_path_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _COLD_LOCKS_BY_LOOP.setdefault(loop, {})
    return per_loop.setdefault(path, asyncio.Lock())


async def _run_cold_recovery(
    *,
    storage_path: Path,
    allow_headless: bool,
    validate: Callable[[httpx.Cookies], Awaitable[None]],
    initial_error: _LoginRedirectError,
) -> ColdRecoveryResult:
    from .cookies import build_httpx_cookies_from_storage
    from .extraction import _LoginRedirectError
    from .storage import snapshot_cookie_jar

    async with _cold_path_lock(storage_path):
        working_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
        snapshot = snapshot_cookie_jar(working_jar)
        generations = _COLD_SUCCESS_GENERATIONS.setdefault(asyncio.get_running_loop(), {})
        last_redirect = initial_error
        if generations.get(storage_path, 0) > 0:
            try:
                await validate(working_jar)
            except _LoginRedirectError as redirect_error:
                last_redirect = redirect_error
            else:
                return ColdRecoveryResult(working_jar, snapshot)

        attempts = (
            lambda: try_headless_reauth(
                storage_path=storage_path,
                cookie_jar=working_jar,
                allow_headless=allow_headless,
            ),
            lambda: try_master_token_reauth(
                storage_path=storage_path,
                cookie_jar=working_jar,
            ),
        )
        for attempt in attempts:
            if not await attempt():
                continue
            snapshot = snapshot_cookie_jar(working_jar)
            try:
                await validate(working_jar)
            except _LoginRedirectError as redirect_error:
                last_redirect = redirect_error
                continue
            generations[storage_path] = generations.get(storage_path, 0) + 1
            return ColdRecoveryResult(working_jar, snapshot)
        raise last_redirect


async def coalesced_cold_recovery(
    *,
    storage_path: Path,
    allow_headless: bool,
    validate: Callable[[httpx.Cookies], Awaitable[None]],
    initial_error: _LoginRedirectError,
) -> ColdRecoveryResult:
    """Share one complete cold ladder across all loops for equivalent callers."""
    canonical_path = canonical_storage_key(storage_path)
    assert canonical_path is not None  # storage_path is a real path here
    # Keyed per (canonical path, rung policy) — the shape the old
    # ``_COLD_INFLIGHT_BY_LOOP`` registry used, now process-global.
    flight_key = (str(canonical_path), ("cold", allow_headless))

    def _factory() -> Coroutine[Any, Any, ColdRecoveryResult]:
        return _run_cold_recovery(
            storage_path=canonical_path,
            allow_headless=allow_headless,
            validate=validate,
            initial_error=initial_error,
        )

    _is_leader, flight = _single_flight.claim(flight_key, _factory)
    shared = await _single_flight.await_flight(flight)
    # Per-call COPIES (CodeRabbit #1): the flight result is shared verbatim across
    # every follower on every loop. Downstream mutates BOTH halves — the jar
    # becomes a caller's live jar (rotated in place) and
    # ``save_cookies_to_storage(original_snapshot=...)`` mutates the snapshot dict —
    # so hand each caller its own jar container and snapshot copy to prevent
    # cross-loop corruption. ``CookieSnapshot`` values are immutable NamedTuples,
    # so a shallow ``dict`` copy fully isolates the mapping.
    from .cookies import _clone_cookie_jar

    return ColdRecoveryResult(_clone_cookie_jar(shared.cookie_jar), dict(shared.snapshot))


async def try_headless_reauth(
    *,
    storage_path: Path | None,
    cookie_jar: httpx.Cookies,
    allow_headless: bool,
) -> bool:
    """Drive opt-in browser recovery and reload the persisted cookie jar."""
    if storage_path is None:
        logger.debug("Headless re-auth skipped: auth has no writable storage path.")
        return False

    from ..paths import get_browser_profile_dir
    from .cookies import _replace_cookie_jar, build_httpx_cookies_from_storage
    from .headless_reauth import HeadlessReauthStatus, attempt_headless_reauth

    result = await asyncio.to_thread(
        attempt_headless_reauth,
        storage_path=storage_path,
        allow_headless=allow_headless,
        browser_profile=get_browser_profile_dir(storage_path=storage_path),
    )
    if result.status is not HeadlessReauthStatus.SUCCESS:
        logger.debug(
            "Headless re-auth did not succeed (%s): %s",
            result.status.value,
            result.reason,
        )
        return False
    try:
        fresh_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Headless re-auth wrote storage but its cookies failed to load (%s).",
            type(exc).__name__,
        )
        return False
    _replace_cookie_jar(cookie_jar, fresh_jar)
    logger.info("Headless re-auth succeeded; reloaded re-minted cookies for retry.")
    return True


async def _run_master_token_reauth(*, storage_path: Path) -> httpx.Cookies | None:
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
    from .cookies import build_httpx_cookies_from_storage
    from .master_token import MasterTokenError, remint_from_stored_token

    try:
        await remint_from_stored_token(storage_path)
    except MasterTokenError as exc:
        logger.warning("Master-token re-mint failed (%s); authentication error stands.", exc)
        return None

    try:
        fresh_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Master-token re-mint could not persist/reload cookies (%s); "
            "authentication error stands.",
            type(exc).__name__,
        )
        return None
    return fresh_jar


async def try_master_token_reauth(*, storage_path: Path | None, cookie_jar: httpx.Cookies) -> bool:
    """Share one L4 re-mint across overlapping cold and live callers, any loop."""
    if storage_path is None:
        return False

    canonical_path = canonical_storage_key(storage_path)
    assert canonical_path is not None  # narrowed non-None above
    # Sole derivation site (notebooklm.paths.master_token_path_for, #2103 PR-1):
    # converges with _app/auth_check.py, cli/services/auth_refresh.py, and
    # cli/master_token_login.py, which previously each derived this sibling
    # independently and disagreed on canonicalization for a symlinked/relative
    # storage path.
    from ..paths import master_token_path_for

    master_token_path = master_token_path_for(canonical_path)
    if not master_token_path.exists():
        return False

    flight_key = (str(canonical_path), "master-token")

    def _factory() -> Coroutine[Any, Any, httpx.Cookies | None]:
        return _run_master_token_reauth(storage_path=canonical_path)

    _is_leader, flight = _single_flight.claim(flight_key, _factory)
    fresh_jar = await _single_flight.await_flight(flight)
    if fresh_jar is None:
        return False

    from .cookies import _clone_cookie_jar, _replace_cookie_jar

    # Repopulate this caller's jar from a COPY of the shared result (CodeRabbit
    # #1): the single-flight jar is handed to every follower on every loop, so
    # cloning before we read it keeps concurrent followers isolated from one
    # another's jar mutation.
    _replace_cookie_jar(cookie_jar, _clone_cookie_jar(fresh_jar))
    logger.info("Master-token re-mint succeeded; reloaded fresh cookies for retry.")
    return True
