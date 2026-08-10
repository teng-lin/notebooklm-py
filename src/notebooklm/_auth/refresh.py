"""Refresh-command coordination and the canonical token-acquisition ladder."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .._env import get_base_url
from ..paths import get_storage_path, resolve_profile
from . import cookies as _auth_cookies
from . import extraction as _auth_extraction
from . import keepalive as _keepalive
from . import paths as _auth_paths
from . import recovery as _auth_recovery
from . import single_flight as _single_flight
from . import storage as _auth_storage
from .account import authuser_query
from .cookie_types import CookieJar
from .paths import resolve_auth_json_env
from .profile_store import ProfileStore
from .storage import get_account_email_for_storage, get_authuser_for_storage

logger = logging.getLogger("notebooklm.auth")

build_httpx_cookies_from_storage = _auth_cookies.build_httpx_cookies_from_storage
_build_httpx_cookies_from_storage_strict = _auth_cookies._build_httpx_cookies_from_storage_strict
_replace_cookie_jar = _auth_cookies._replace_cookie_jar
_cookie_map_from_jar = _auth_cookies._cookie_map_from_jar
build_cookie_jar = _auth_cookies.build_cookie_jar
flatten_cookie_map = _auth_cookies.flatten_cookie_map
_update_cookie_input = _auth_cookies._update_cookie_input
snapshot_cookie_jar = _auth_storage.snapshot_cookie_jar
extract_csrf_from_html = _auth_extraction.extract_csrf_from_html
extract_session_id_from_html = _auth_extraction.extract_session_id_from_html
# Shared URL-only failure classifier — the single source of truth for the
# gate -> cookie-mismatch -> auth-redirect precedence used by both this module's
# pre-check and the extractors themselves. It supersedes the former ``_safe_url``
# / ``_cookie_mismatch_message`` aliases here: those existed only for the
# hand-rolled pre-check this module used to carry, and message formatting now
# lives entirely behind the classifier.
_url_only_extraction_failure = _auth_extraction._url_only_extraction_failure

# Env-var names live in ``_auth.paths``; aliased so the refresh bodies can
# reference them without an extra hop.
NOTEBOOKLM_REFRESH_CMD_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_ENV
NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV
NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV
NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV
_REFRESH_ATTEMPTED_ENV = _auth_paths._REFRESH_ATTEMPTED_ENV

# First-party secret / credential-equivalent env vars scrubbed from the refresh
# command's subprocess environment before spawn (audit refresh-6). The refresh
# command inherits the parent env so it can find PATH/HOME/proxy config and
# re-invoke this library, but MUST NOT inherit our own credential material:
#   * NOTEBOOKLM_AUTH_JSON        — inline Playwright storage_state (the child
#                                   gets the on-disk path instead; #1274).
#   * NOTEBOOKLM_SERVER_TOKEN     — REST server bearer token.
#   * NOTEBOOKLM_SERVER_TOKEN_FILE — path to the REST server bearer token file.
#   * NOTEBOOKLM_MCP_TOKEN        — MCP server bearer token.
#   * NOTEBOOKLM_MCP_OAUTH_PASSWORD — MCP OAuth static password.
# These matter especially once the rung fires MID-SESSION in a long-lived
# server (the server's own auth secret would otherwise leak into every refresh
# subprocess and its grandchildren, visible via ``/proc/<pid>/environ``).
_REFRESH_CMD_SCRUBBED_SECRET_ENV = (
    "NOTEBOOKLM_AUTH_JSON",
    "NOTEBOOKLM_SERVER_TOKEN",
    "NOTEBOOKLM_SERVER_TOKEN_FILE",
    "NOTEBOOKLM_MCP_TOKEN",
    "NOTEBOOKLM_MCP_OAUTH_PASSWORD",
    # Pointer to the 0600 file holding issued OAuth tokens + the client
    # registry — same secret-file-pointer class as SERVER_TOKEN_FILE above.
    "NOTEBOOKLM_MCP_OAUTH_STATE_PATH",
)

# ``_poke_session`` lives in ``_auth.keepalive``; aliased here as a local bare
# name because refresh bodies call it directly. Tests that need to substitute it
# should patch ``notebooklm._auth.refresh._poke_session``.
_poke_session = _keepalive._poke_session


# --- Refresh-coalescing state ------------------------------------------------
# The ContextVar prevents same-task retry loops in the parent process. The env
# flag is passed only to child refresh commands so recursive CLI calls skip refresh.
_REFRESH_ATTEMPTED_CONTEXT: ContextVar[bool] = ContextVar(
    "_REFRESH_ATTEMPTED_CONTEXT", default=False
)

# Cross-loop coalescing + the per-path success epoch now live in
# ``notebooklm._auth.single_flight`` (c-PR2). The old per-loop future maps and
# module-level generation dict (``_REFRESH_INFLIGHT_BY_LOOP`` /
# ``_REFRESH_GENERATIONS`` / ``_REFRESH_LOCKS_BY_LOOP`` / ``_get_refresh_lock``)
# were deleted in the same PR that introduced the shared core; there is no
# revert-hazard split. A single refresh-cmd "rung policy" discriminator keys the
# flight; the success epoch is keyed per canonical PATH only (see
# ``single_flight.py``).
_REFRESH_FLIGHT_POLICY = "refresh-cmd"

# Cap on how many times a single caller FOLLOWS a failing refresh-cmd leader
# before surfacing the failure, so a persistently-failing command cannot make one
# caller wait out an unbounded chain of other callers' subprocesses (CodeRabbit).
# A direct leader-failure and the success-epoch reload short-circuit before this.
_MAX_REFRESH_FOLLOW_RETRIES = 3

# Bounded async wait for another process's refresh flock to release (flock-loser
# reloads-fresh-not-stale fix); lives next to the flock primitive in ``keepalive``.
_wait_for_refresh_holder = _keepalive._wait_for_refresh_holder
canonical_storage_key = _auth_paths.canonical_storage_key


# --- Injected collaborators (plan §7 deps record) -----------------------------
# ``_file_lock_try_exclusive = _keepalive._file_lock_try_exclusive`` and
# ``_refresh_lock_path = _auth_paths._refresh_lock_path`` used to sit here as
# module-scope aliases whose only *documented* purpose was "tests substitute
# this by patching the bare name on this module". That patching protocol is
# replaced by the record below: the three collaborators the refresh-cmd rung's
# tests actually replace — the subprocess runner, the flock acquirer, and the
# lock-path deriver — are threaded keyword-only from the entry points down to
# the leader body, so a test constructs a record instead of mutating module
# state that pytest may or may not restore.


class _RefreshFlock(Protocol):
    """Non-blocking exclusive flock: a context manager yielding "acquired?"."""

    def __call__(self, lock_path: Path) -> AbstractContextManager[bool]: ...


@dataclass(frozen=True, slots=True)
class RefreshCmdDeps:
    """Collaborators of the refresh-cmd rung, injectable per call.

    Every field is optional and ``None`` means "use production". The production
    value is resolved **late**, through this module's namespace, at the moment
    of use (:meth:`runner`, :meth:`flock`, :meth:`lock_path`) — deliberately
    NOT captured into a field default at import time. Two reasons:

    1. a frozen record built at import time would freeze the function objects,
       silently defeating the module-attribute patch seam the whitebox
       concurrency suites still use for the collaborators this record does not
       carry; and
    2. it keeps a partially-specified record useful — ``RefreshCmdDeps(
       run_refresh_cmd=fake)`` overrides one collaborator and leaves the other
       two on production, which is how nearly every test wants to use it.
    """

    run_refresh_cmd: Callable[[Path, str | None], Coroutine[Any, Any, None]] | None = None
    acquire_refresh_flock: _RefreshFlock | None = None
    derive_refresh_lock_path: Callable[[Path | None], Path | None] | None = None

    def runner(self) -> Callable[[Path, str | None], Coroutine[Any, Any, None]]:
        """The ``NOTEBOOKLM_REFRESH_CMD`` subprocess runner."""
        if self.run_refresh_cmd is not None:
            return self.run_refresh_cmd
        return _run_refresh_cmd

    def flock(self) -> _RefreshFlock:
        """The cross-process refresh flock acquirer ([refresh-2])."""
        if self.acquire_refresh_flock is not None:
            return self.acquire_refresh_flock
        return _keepalive._file_lock_try_exclusive

    def lock_path(self) -> Callable[[Path | None], Path | None]:
        """The per-storage-path refresh lock-file deriver."""
        if self.derive_refresh_lock_path is not None:
            return self.derive_refresh_lock_path
        return _auth_paths._refresh_lock_path


# One shared empty record, so the default path allocates nothing per call. It
# carries no captured functions (every field is ``None``), so it cannot stale.
_PRODUCTION_REFRESH_CMD_DEPS = RefreshCmdDeps()


async def _refresh_cmd_leader_body(
    path_key: str,
    resolved_storage_path: Path,
    profile: str | None,
    *,
    deps: RefreshCmdDeps = _PRODUCTION_REFRESH_CMD_DEPS,
) -> None:
    """Leader-only body: run ``_run_refresh_cmd`` under a cross-process flock.

    Runs as the single-flight leader's ``asyncio.Task``. In-process coalescing
    already guarantees one subprocess per process; the per-path flock adds the
    CROSS-process exclusion that closes the refresh-cmd stampede ([refresh-2]),
    mirroring the keepalive rotation flock. Re-mint rungs deliberately take NO
    such flock (each cross-process re-mint owns its own browser — §c.5/§c.7).

    On a genuine cross-process contention (another process holds the flock and
    is refreshing) the subprocess is skipped and — rather than reload the STALE
    file immediately (the holder has not finished writing yet) — the loser WAITS
    (bounded) for the holder to release, then the caller reloads the fresh
    cookies it wrote (:func:`_wait_for_refresh_holder`). The success epoch is
    bumped ONLY after our OWN subprocess actually exits zero, so a skipped
    (wait-then-reload) or failed attempt leaves waiters retrying (single_flight
    guarantee 2).
    """
    lock_path = deps.lock_path()(resolved_storage_path)
    if lock_path is None:
        await deps.runner()(resolved_storage_path, profile)
        _single_flight.note_success(path_key)
        return
    with deps.flock()(lock_path) as acquired:
        if acquired:
            await deps.runner()(resolved_storage_path, profile)
            _single_flight.note_success(path_key)
            return
    # Lost the cross-process flock: another process holds it and is refreshing
    # right now. Wait (bounded) for it to finish writing, THEN return so the
    # caller reloads the FRESH file rather than the stale one it would otherwise
    # re-read immediately. No subprocess runs and the epoch is NOT bumped here.
    logger.debug(
        "%s: %s held by another process; waiting for it to finish",
        NOTEBOOKLM_REFRESH_CMD_ENV,
        lock_path,
    )
    await _wait_for_refresh_holder(lock_path)


async def _coalesced_run_refresh_cmd(
    refresh_key: str,
    resolved_storage_path: Path,
    profile: str | None,
    *,
    deps: RefreshCmdDeps = _PRODUCTION_REFRESH_CMD_DEPS,
) -> None:
    """Run the refresh-cmd once across all loops for ``refresh_key``.

    Delegates coalescing + the success-epoch skip to
    :mod:`notebooklm._auth.single_flight`. Preserves the historical None/raise
    contract: returns ``None`` when the storage is (or has just been) refreshed
    and the caller should reload; raises the subprocess exception when the
    refresh genuinely failed and no concurrent flight succeeded; propagates
    ``CancelledError`` on caller-side cancellation (after settling the shared
    subprocess).

    Preserved single-flight guarantees:

    1. **Late-waiter skip (compare-under-exclusion)** — the success epoch is
       captured BEFORE waiting, and the epoch compare + the flight claim happen
       under a SINGLE owner ``_lock`` hold via
       ``single_flight.claim_if_epoch_current``. A caller whose ``epoch_before``
       is already stale (a sibling succeeded and prompt-popped its flight in the
       meantime) gets a skip signal and reloads instead of spawning a redundant
       subprocess.
    2. **Bump on subprocess success only** — the leader body bumps the epoch
       exclusively after ``_run_refresh_cmd`` exits zero; a failed (or
       flock-skipped) attempt leaves the epoch untouched so waiters retry.
    3. **Cancel/settle race** — ``await_flight`` settles the shared subprocess
       before propagating caller cancellation; a subprocess that fails while the
       caller is cancelled never bumps the epoch (the body raised before the
       bump).
    4. **No phantom bump on cancel-before-work** — only the leader body bumps,
       and only after real work; a caller cancelled before/while awaiting never
       bumps, and neither does a follower.
    """
    # ``refresh_key`` is already the canonical ``str(resolved_storage_path)``
    # computed by the caller; reuse it as the per-path success-epoch key rather
    # than recomputing.
    path_key = refresh_key
    flight_key = (path_key, _REFRESH_FLIGHT_POLICY)
    # Capture the epoch BEFORE any wait (guarantee 1). A concurrent flight that
    # SUCCEEDS while we wait bumps past this baseline, letting us skip.
    epoch_before = _single_flight.read_success_epoch(path_key)

    def _factory() -> Coroutine[Any, Any, None]:
        return _refresh_cmd_leader_body(path_key, resolved_storage_path, profile, deps=deps)

    # A PERSISTENTLY failing refresh-cmd with N concurrent waiters would run up to
    # N sequential leader subprocesses (each failed leader's follower becomes a
    # fresh leader). The first success bumps the epoch and remaining waiters skip.
    # The follow-then-fail retries are capped at ``_MAX_REFRESH_FOLLOW_RETRIES`` so
    # one caller cannot wait out an unbounded chain; direct leader-failure
    # propagation and the success-epoch reload are preserved.
    follow_failures = 0
    while True:
        claimed = _single_flight.claim_if_epoch_current(
            flight_key, _factory, path_key=path_key, epoch_before=epoch_before
        )
        if claimed is None:
            # Compare-under-exclusion skip: a sibling already succeeded (epoch
            # advanced) — reload the fresh storage instead of refreshing.
            return
        is_leader, flight = claimed
        try:
            await _single_flight.await_flight(flight)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - inspect epoch / leadership below
            if _single_flight.read_success_epoch(path_key) > epoch_before:
                # A concurrent flight succeeded even though the one we awaited
                # failed — reload rather than propagate.
                return
            if is_leader:
                # Our own subprocess failed and nobody else refreshed —
                # propagate so the caller surfaces the failure.
                raise
            # We followed a leader whose subprocess failed. Retry as a fresh
            # leader, but bound the chain (see ``_MAX_REFRESH_FOLLOW_RETRIES``).
            follow_failures += 1
            if follow_failures >= _MAX_REFRESH_FOLLOW_RETRIES:
                raise
            continue
        else:
            # Flight succeeded (leader bumped the epoch) — caller reloads.
            return


def _midsession_refresh_cmd_enabled() -> bool:
    """True iff the L2.5 refresh-cmd rung may fire in the MID-SESSION ladder.

    Gated on BOTH the refresh command being configured (``NOTEBOOKLM_REFRESH_CMD``)
    AND the one-release opt-in ``NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1`` (audit
    refresh-4). The opt-in defaults OFF for one release to de-risk operators
    whose commands assume cold-start-only invocation; a later release flips it
    default-on. The same recursion guards as the cold path
    (:func:`_should_try_refresh`) apply so a refresh command that re-invokes
    this library cannot spawn a nested refresh subprocess.
    """
    if _REFRESH_ATTEMPTED_CONTEXT.get() or os.environ.get(_REFRESH_ATTEMPTED_ENV) == "1":
        return False
    if not os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV):
        return False
    return os.environ.get(NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV) == "1"


async def try_refresh_cmd_reauth(
    *,
    storage_path: Path | None,
    cookie_jar: httpx.Cookies,
    profile: str | None = None,
    deps: RefreshCmdDeps = _PRODUCTION_REFRESH_CMD_DEPS,
) -> bool:
    """L2.5 mid-session rung: run ``NOTEBOOKLM_REFRESH_CMD`` and reload cookies.

    Client-neutral adapter mirroring :func:`notebooklm._auth.recovery.try_headless_reauth`
    / :func:`~notebooklm._auth.recovery.try_master_token_reauth` so the
    mid-session ladder (``_auth/session.py:refresh_auth_session``) can slot the
    refresh-cmd rung between L2 (RotateCookies) and L3 (headless re-mint). It
    REUSES the same cold-start machinery — the ``single_flight``-coalesced
    :func:`_coalesced_run_refresh_cmd` and the per-path refresh flock
    ([refresh-2]) — rather than forking a second implementation, and honours the
    same env contract (command string, ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL``
    opt-in, timeout, secret-scrub).

    Opt-in for one release via ``NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1`` (default
    OFF — see :func:`_midsession_refresh_cmd_enabled`).

    Returns ``True`` when the command ran (or a concurrent flight refreshed the
    storage) and fresh cookies were reloaded into ``cookie_jar`` — the caller
    retries the homepage GET once. Returns ``False`` when the rung is
    unavailable (no opt-in / no command / no storage file) or the command
    failed; the caller's original dead-cookie error then stands, exactly as when
    the rung is off (byte-identical default behaviour).
    """
    if storage_path is None:
        return False
    if not _midsession_refresh_cmd_enabled():
        return False
    canonical_path = canonical_storage_key(storage_path)
    assert canonical_path is not None  # narrowed non-None above
    refresh_key = str(canonical_path)
    # In-process recursion guard, same discipline as the cold path. It is set for
    # the DURATION of the coalesced run and reset in the ``finally`` BEFORE this
    # function returns, so it guards only re-entry WHILE the subprocess is in
    # flight (a refresh command re-invoking this library on the same task), NOT
    # the caller's later retried GET (which runs after the reset). That later GET
    # is safe regardless: the ladder invokes each rung once per refresh run.
    refresh_token = _REFRESH_ATTEMPTED_CONTEXT.set(True)
    try:
        # Coalesced across loops + serialized across processes by the flock.
        await _coalesced_run_refresh_cmd(refresh_key, canonical_path, profile, deps=deps)
        fresh_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, canonical_path)
    except asyncio.CancelledError:
        raise
    except (RuntimeError, OSError, ValueError) as exc:
        logger.warning(
            "Mid-session %s rung failed (%s); authentication error stands.",
            NOTEBOOKLM_REFRESH_CMD_ENV,
            type(exc).__name__,
        )
        return False
    finally:
        _REFRESH_ATTEMPTED_CONTEXT.reset(refresh_token)
    _replace_cookie_jar(cookie_jar, fresh_jar)
    logger.info(
        "Mid-session %s rung succeeded; reloaded refreshed cookies for retry.",
        NOTEBOOKLM_REFRESH_CMD_ENV,
    )
    return True


_AUTH_ERROR_SIGNALS = (
    "authentication expired",
    "redirected to",
    "run 'notebooklm login'",
)


def _should_try_refresh(err: Exception) -> bool:
    """True when an auth failure should trigger NOTEBOOKLM_REFRESH_CMD."""
    if _REFRESH_ATTEMPTED_CONTEXT.get() or os.environ.get(_REFRESH_ATTEMPTED_ENV) == "1":
        return False
    if not os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV):
        return False
    msg = str(err).lower()
    return any(sig in msg for sig in _AUTH_ERROR_SIGNALS)


def _split_refresh_cmd(cmd: str) -> list[str]:
    """Parse ``NOTEBOOKLM_REFRESH_CMD`` into an argv for ``shell=False`` exec.

    On POSIX systems, defers to :func:`shlex.split`. On Windows, uses
    ``CommandLineToArgvW`` so quoted paths like
    ``"C:\\Program Files\\Python\\python.exe"`` produce a properly unquoted
    argv that ``subprocess.run(argv, shell=False)`` can locate. ``shlex``
    in non-POSIX mode preserves the literal quote characters and would
    leave the OS unable to find the executable.

    Raises:
        ValueError: If the command is malformed (e.g., unterminated quote).
    """
    if os.name != "nt":
        return shlex.split(cmd)

    import ctypes
    from ctypes import wintypes

    CommandLineToArgvW = ctypes.windll.shell32.CommandLineToArgvW  # type: ignore[attr-defined]
    CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    LocalFree = ctypes.windll.kernel32.LocalFree  # type: ignore[attr-defined]
    LocalFree.argtypes = [wintypes.HLOCAL]
    LocalFree.restype = wintypes.HLOCAL

    argc = ctypes.c_int(0)
    argv_ptr = CommandLineToArgvW(cmd, ctypes.byref(argc))
    if not argv_ptr:
        # CommandLineToArgvW returns NULL for some empty-input edge cases.
        # Mirror shlex.split's behavior and return an empty list; the caller
        # surfaces this as ``RuntimeError("...parsed to empty argv")``.
        return []
    try:
        # On Windows, ``CommandLineToArgvW`` is documented to return a single
        # empty-string entry (argc=1, argv[0]="") for whitespace-only input,
        # rather than NULL. Filter out empty entries so the caller's
        # ``if not argv`` empty-argv guard catches this case the same way
        # ``shlex.split("   ") == []`` does on POSIX.
        return [argv_ptr[i] for i in range(argc.value) if argv_ptr[i]]
    finally:
        LocalFree(ctypes.cast(argv_ptr, wintypes.HLOCAL))


async def _run_refresh_cmd(storage_path: Path | None = None, profile: str | None = None) -> None:
    """Run ``NOTEBOOKLM_REFRESH_CMD`` to refresh stored cookies.

    By default, the command string is parsed with :func:`shlex.split` and
    executed with ``shell=False`` to avoid shell-injection footguns when the
    env var is sourced from CI configs or container env files. Set
    ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1`` to opt back into the legacy
    ``shell=True`` behavior (e.g., when the command relies on shell features
    like pipes, redirection, or env-var expansion).

    Environment forwarding:
        The refresh command inherits the **full parent environment** (so it
        can find ``PATH``, ``HOME``, proxy settings, and any custom config the
        operator relies on), with these deliberate adjustments:

        * A fixed set of **first-party secret / credential-equivalent** env
          vars is **scrubbed** (see :data:`_REFRESH_CMD_SCRUBBED_SECRET_ENV`):
          ``NOTEBOOKLM_AUTH_JSON`` (inline storage_state — the child gets the
          on-disk path via ``NOTEBOOKLM_REFRESH_STORAGE_PATH`` instead),
          ``NOTEBOOKLM_SERVER_TOKEN`` / ``NOTEBOOKLM_SERVER_TOKEN_FILE`` (REST
          server bearer token), ``NOTEBOOKLM_MCP_TOKEN``,
          ``NOTEBOOKLM_MCP_OAUTH_PASSWORD`` (MCP server auth), and
          ``NOTEBOOKLM_MCP_OAUTH_STATE_PATH`` (pointer to the 0600 issued-token /
          client-registry file). None of these is needed by a refresh command,
          and none should leak down the process tree.
        * ``_NOTEBOOKLM_REFRESH_ATTEMPTED`` is injected as the recursion guard.
        * ``NOTEBOOKLM_REFRESH_PROFILE`` / ``NOTEBOOKLM_REFRESH_STORAGE_PATH``
          are injected so the command refreshes the right profile in place.

        SECURITY: only the first-party secrets above are scrubbed. We
        deliberately do **not** impose a hard allowlist, because a refresh
        command commonly re-invokes this library and legitimately needs much of
        the inherited env. As a consequence, **any other secret in the
        launching shell** (e.g. ``GOOGLE_*`` tokens, CI secrets, API keys — and
        any token the operator embeds in ``NOTEBOOKLM_REFRESH_CMD`` itself) is
        inherited by the refresh command and every grandchild it spawns, and is
        visible via ``/proc/<pid>/environ`` to the same UID. Operators MUST NOT
        keep unrelated secrets in the environment that launches the refresh
        command; scope secrets to the processes that need them.

        SERVER CONTEXT: this function can now fire MID-SESSION inside a
        long-lived REST/MCP server (the L2.5 rung — see
        :func:`try_refresh_cmd_reauth`). Scrubbing the server's own auth
        secrets (``NOTEBOOKLM_SERVER_TOKEN`` etc.) closes the path by which a
        mid-session refresh would otherwise hand the server's credential to an
        operator-supplied subprocess. The redaction of captured stdout/stderr
        (below) likewise limits what a mid-session refresh writes to logs.

    Raises:
        RuntimeError: If the refresh command is missing, parses to an empty
            argv, is malformed (unterminated quote), times out, or exits
            non-zero.
    """
    cmd = os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV)
    if not cmd:
        raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} is not set; cannot refresh cookies.")
    # Forward the full parent env (PATH/HOME/proxy/etc. the command needs),
    # minus the one credential-equivalent var below. See the "Environment
    # forwarding" section of this function's docstring for the SECURITY note
    # on why a hard allowlist is deliberately avoided (issue #1274).
    refresh_env = os.environ.copy()
    # Scrub the first-party secret / credential-equivalent env vars before
    # spawn (audit refresh-6). ``NOTEBOOKLM_AUTH_JSON`` carries the full
    # Playwright storage_state; ``NOTEBOOKLM_SERVER_TOKEN`` /
    # ``NOTEBOOKLM_SERVER_TOKEN_FILE`` / ``NOTEBOOKLM_MCP_TOKEN`` /
    # ``NOTEBOOKLM_MCP_OAUTH_PASSWORD`` / ``NOTEBOOKLM_MCP_OAUTH_STATE_PATH`` are
    # our own server auth secrets (the last a pointer to the 0600 token file).
    # Forwarding any of them via ``os.environ.copy()`` would inherit them down
    # the tree (visible via ``/proc/<pid>/environ`` to the same UID) and into
    # any grandchild the refresh command spawns — a real exposure now that this
    # rung can fire mid-session inside a long-lived server. None is needed by
    # the child: it receives the canonical on-disk storage path via
    # ``NOTEBOOKLM_REFRESH_STORAGE_PATH`` (set just below). The rest
    # (PROFILE/HOME/BASE_URL/...) are config the child may legitimately consume
    # when it re-invokes this library, so they stay. Any token an operator
    # embeds in ``NOTEBOOKLM_REFRESH_CMD`` is intentionally inherited — see this
    # function's docstring SECURITY note.
    for _secret_var in _REFRESH_CMD_SCRUBBED_SECRET_ENV:
        refresh_env.pop(_secret_var, None)
    refresh_env[_REFRESH_ATTEMPTED_ENV] = "1"
    refresh_env["NOTEBOOKLM_REFRESH_PROFILE"] = resolve_profile(profile)
    refresh_env["NOTEBOOKLM_REFRESH_STORAGE_PATH"] = str(
        storage_path or get_storage_path(profile=profile)
    )

    use_shell = os.environ.get(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV) == "1"
    run_target: str | list[str]
    run_shell: bool
    if use_shell:
        logger.warning("Using shell-mode for %s (opt-in)", NOTEBOOKLM_REFRESH_CMD_ENV)
        # Deliberately do NOT log a basename/preview of ``cmd`` here: in
        # shell-mode the entire string is forwarded to ``/bin/sh -c`` and
        # may contain pipes, redirection, ``$VAR`` expansion, or inline
        # tokens. We can't extract a single "first token" without risking
        # leaking the rest, so we stay silent past the opt-in warning.
        run_target = cmd
        run_shell = True
    else:
        try:
            # POSIX → shlex.split. Windows → CommandLineToArgvW so quoted
            # paths like ``"C:\\Program Files\\..."`` arrive unquoted.
            argv = _split_refresh_cmd(cmd)
        except ValueError as split_err:
            raise RuntimeError(
                f"{NOTEBOOKLM_REFRESH_CMD_ENV} could not be parsed: {split_err}"
            ) from split_err
        if not argv:
            raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} parsed to empty argv")
        # Log basename only — full argv may carry tokens and absolute paths
        # can leak secrets-directory layouts.
        logger.info("Running refresh command: %s ...", os.path.basename(argv[0]))
        run_target = argv
        run_shell = False

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            run_target,
            shell=run_shell,
            capture_output=True,
            text=True,
            timeout=60,
            env=refresh_env,
        )
    except (subprocess.TimeoutExpired, OSError) as refresh_err:
        raise RuntimeError(
            f"{NOTEBOOKLM_REFRESH_CMD_ENV} failed to execute: {refresh_err}"
        ) from refresh_err
    if result.returncode != 0:
        # Do NOT interpolate stdout/stderr into the user-facing raise.
        # Subprocesses commonly print bearer tokens, cookies, and absolute
        # paths into a user's credentials directory. ``RuntimeError`` bubbles
        # up through ``cli.error_handler`` and lands on stderr (or a JSON
        # envelope), which is the wrong audience for that material.
        #
        # Two-channel disclosure: the user sees only exit code + executable
        # basename; developers running with ``-vv`` get the full output
        # through the package's redacting DEBUG logger.
        # In shell-mode ``run_target`` is the raw command STRING, not a
        # list. Extract the basename of its first token
        # so users still see a useful script name (the string is user-supplied
        # and not a secret — its argv[0] equivalent is safe to surface).
        if isinstance(run_target, list) and run_target:
            executable_basename = os.path.basename(run_target[0])
        elif isinstance(run_target, str) and run_target.strip():
            executable_basename = os.path.basename(run_target.split()[0])
        else:
            executable_basename = "shell"
        # Default DEBUG line is metadata-only (audit refresh-8): basename +
        # exit code + byte counts, never the raw captured output. A refresh
        # command commonly prints bearer tokens / cookies / credentials-dir
        # paths, and the redaction filter only collapses KNOWN credential
        # shapes — so dumping raw ``%r`` here would surface anything the filter
        # doesn't recognise. This matters more now the rung can fire
        # mid-session in a long-lived server whose DEBUG log is retained.
        stdout_bytes = len((result.stdout or "").encode("utf-8", "replace"))
        stderr_bytes = len((result.stderr or "").encode("utf-8", "replace"))
        logger.debug(
            "%s exited %d (executable=%s, stdout=%d bytes, stderr=%d bytes)",
            NOTEBOOKLM_REFRESH_CMD_ENV,
            result.returncode,
            executable_basename,
            stdout_bytes,
            stderr_bytes,
        )
        # Full captured output is available ONLY behind an explicit opt-in env
        # (``NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT=1``). It still flows through the
        # package's redacting DEBUG logger (credential SHAPES collapse to
        # ``***``); the opt-in exists for local diagnosis and defaults OFF so a
        # server operator does not passively accumulate command output in logs.
        if os.environ.get(NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV) == "1":
            logger.debug(
                "%s captured output (%s=1): stdout=%r stderr=%r",
                NOTEBOOKLM_REFRESH_CMD_ENV,
                NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV,
                result.stdout,
                result.stderr,
            )
        raise RuntimeError(
            f"{NOTEBOOKLM_REFRESH_CMD_ENV} exited {result.returncode} "
            f"(executable: {executable_basename}). "
            f"Set {NOTEBOOKLM_REFRESH_CMD_LOG_OUTPUT_ENV}=1 and re-run with -vv to see "
            "the command's captured stdout/stderr in the debug log."
        )
    logger.info("NotebookLM cookies refreshed via %s", NOTEBOOKLM_REFRESH_CMD_ENV)


# --- Token-route resolution (absorbed from _auth/headers.py) -----------------
# Most authuser helpers live in :mod:`notebooklm._auth.account` (wire formatters)
# and :mod:`notebooklm._auth.storage` (record readers). What follows is the *routing*
# glue that combines them for the token-fetch entry points below, preserving
# explicit caller intent vs. resolved-from-storage defaults. It used to live in
# a 68-line ``_auth/headers.py`` whose only three call sites are in this module;
# ADR-0033's sanctioned merge folded it in.


def _resolve_token_route_kwargs(
    storage_path: Path | None,
    *,
    authuser: int | None,
    account_email: str | None,
) -> dict[str, Any]:
    """Resolve token-fetch routing while preserving explicit caller intent."""
    explicit_authuser = authuser is not None
    env_auth_present = storage_path is None and resolve_auth_json_env() is not None
    env_authuser = 0
    env_account_email: str | None = None
    if env_auth_present and authuser is None:
        from .cookies import _load_storage_state

        try:
            state = _load_storage_state(None)
            metadata = _auth_storage.read_account_metadata_from_storage_state(state)
        except (OSError, ValueError, TypeError):
            metadata = {}
        raw_authuser = metadata.get("authuser")
        raw_email = metadata.get("email")
        if type(raw_authuser) is int and raw_authuser >= 0:
            env_authuser = raw_authuser
        if isinstance(raw_email, str) and raw_email.strip():
            env_account_email = raw_email.strip()

    resolved_authuser = (
        authuser
        if authuser is not None
        else env_authuser
        if env_auth_present
        else get_authuser_for_storage(storage_path)
    )
    if account_email is not None:
        resolved_account_email = account_email
    elif explicit_authuser:
        resolved_account_email = None
    else:
        resolved_account_email = (
            env_account_email if env_auth_present else get_account_email_for_storage(storage_path)
        )

    route_kwargs: dict[str, Any] = {"authuser": resolved_authuser}
    if resolved_account_email is not None:
        route_kwargs["account_email"] = resolved_account_email
    if explicit_authuser:
        route_kwargs["force_authuser_query"] = True
    return route_kwargs


async def _fetch_tokens_with_refresh(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
    force_authuser_query: bool = False,
    allow_headless: bool = False,
    env_auth: bool = False,
    deps: RefreshCmdDeps = _PRODUCTION_REFRESH_CMD_DEPS,
) -> tuple[str, str, bool, _auth_storage.CookieSnapshot | None]:
    """Compatibility four-tuple projection over the one complete token ladder."""
    explicit_authuser = (
        authuser if authuser is not None and (authuser != 0 or force_authuser_query) else None
    )

    async def resolve_route(path: Path | None) -> dict[str, Any]:
        return _resolve_token_route_kwargs(
            path,
            authuser=explicit_authuser,
            account_email=account_email,
        )

    async def load_replacement(
        path: Path,
    ) -> tuple[httpx.Cookies, _auth_storage.CookieSnapshot, CookieJar | None]:
        live = await asyncio.to_thread(build_httpx_cookies_from_storage, path)
        return live, snapshot_cookie_jar(live), None

    result = await _fetch_tokens_with_refresh_core(
        cookie_jar,
        storage_path,
        profile,
        resolve_route=resolve_route,
        load_replacement=load_replacement,
        initial_baseline=None,
        env_auth=env_auth,
        allow_headless=allow_headless,
        deps=deps,
    )
    return result[0], result[1], result[2], result[3]


async def _fetch_tokens_with_exact_baseline(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None,
    profile: str | None,
    *,
    initial_baseline: CookieJar,
    resolve_route: Callable[[Path | None], Coroutine[Any, Any, dict[str, Any]]],
    allow_headless: bool,
    env_auth: bool,
) -> tuple[str, str, CookieJar]:
    """Typed projection retaining the exact raw-sample replacement baseline."""

    async def load_replacement(
        path: Path,
    ) -> tuple[httpx.Cookies, _auth_storage.CookieSnapshot, CookieJar | None]:
        pair = await asyncio.to_thread(_auth_cookies._build_cookie_pair_from_storage, path)
        return pair.live, snapshot_cookie_jar(pair.live), pair.baseline

    result = await _fetch_tokens_with_refresh_core(
        cookie_jar,
        storage_path,
        profile,
        resolve_route=resolve_route,
        load_replacement=load_replacement,
        initial_baseline=initial_baseline,
        env_auth=env_auth,
        allow_headless=allow_headless,
    )
    baseline = result[4]
    if baseline is None:  # pragma: no cover - typed initial baseline is always retained
        raise AssertionError("typed token load lost its persistence baseline")
    return result[0], result[1], baseline


async def _fetch_tokens_with_refresh_core(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None,
    profile: str | None,
    *,
    resolve_route: Callable[[Path | None], Coroutine[Any, Any, dict[str, Any]]],
    load_replacement: Callable[
        [Path],
        Coroutine[
            Any,
            Any,
            tuple[httpx.Cookies, _auth_storage.CookieSnapshot, CookieJar | None],
        ],
    ],
    initial_baseline: CookieJar | None,
    env_auth: bool,
    allow_headless: bool,
    deps: RefreshCmdDeps = _PRODUCTION_REFRESH_CMD_DEPS,
) -> tuple[str, str, bool, _auth_storage.CookieSnapshot | None, CookieJar | None]:
    """Own the sole initial/L2.5/L3/L4 token acquisition ladder."""
    try:
        csrf, session_id = await _fetch_tokens_with_jar(
            cookie_jar,
            storage_path,
            **await resolve_route(storage_path),
        )
        return csrf, session_id, False, None, initial_baseline
    except ValueError as err:
        return await _cold_fallbacks(
            err,
            cookie_jar,
            storage_path,
            profile,
            env_auth=env_auth,
            allow_headless=allow_headless,
            resolve_route=resolve_route,
            load_replacement=load_replacement,
            baseline=initial_baseline,
            deps=deps,
        )


async def _cold_fallbacks(
    err: ValueError,
    cookie_jar: httpx.Cookies,
    storage_path: Path | None,
    profile: str | None,
    *,
    env_auth: bool,
    allow_headless: bool,
    resolve_route: Callable[[Path | None], Coroutine[Any, Any, dict[str, Any]]],
    load_replacement: Callable[
        [Path],
        Coroutine[
            Any,
            Any,
            tuple[httpx.Cookies, _auth_storage.CookieSnapshot, CookieJar | None],
        ],
    ],
    baseline: CookieJar | None,
    deps: RefreshCmdDeps = _PRODUCTION_REFRESH_CMD_DEPS,
) -> tuple[str, str, bool, _auth_storage.CookieSnapshot | None, CookieJar | None]:
    """Run the L2.5 refresh command, then the coalesced L3/L4 recovery."""

    def should_try_refresh(initial_error: Exception, is_env_auth: bool) -> bool:
        eligible = _should_try_refresh(initial_error)
        if eligible and is_env_auth:
            logger.debug("Skipping %s: env auth has no file", NOTEBOOKLM_REFRESH_CMD_ENV)
            return False
        return eligible

    def resolve_refresh_path(initial_error: ValueError) -> Path:
        logger.warning(
            "NotebookLM auth failed (%s). Running %s to refresh cookies.",
            initial_error,
            NOTEBOOKLM_REFRESH_CMD_ENV,
        )
        refresh_path = canonical_storage_key(storage_path or get_storage_path(profile=profile))
        assert refresh_path is not None
        return refresh_path

    async def run_refresh_attempt(
        path: Path,
    ) -> tuple[str, str, _auth_storage.CookieSnapshot, CookieJar | None]:
        refresh_token = _REFRESH_ATTEMPTED_CONTEXT.set(True)
        try:
            try:
                await _coalesced_run_refresh_cmd(str(path), path, profile, deps=deps)
                fresh_jar, snapshot, new_baseline = await load_replacement(path)
                _replace_cookie_jar(cookie_jar, fresh_jar)
                route = await resolve_route(path)
                csrf, session_id = await _fetch_tokens_with_jar(cookie_jar, path, **route)
                return csrf, session_id, snapshot, new_baseline
            except (RuntimeError, OSError, ValueError) as rung_error:
                logger.warning(
                    "%s rung failed (%s); falling through to the re-mint rungs.",
                    NOTEBOOKLM_REFRESH_CMD_ENV,
                    rung_error,
                )
                raise
        finally:
            _REFRESH_ATTEMPTED_CONTEXT.reset(refresh_token)

    async def validate_recovered(jar: httpx.Cookies) -> None:
        await _fetch_tokens_with_jar(jar, storage_path, **await resolve_route(storage_path))

    async def fetch_recovered(jar: httpx.Cookies) -> tuple[str, str]:
        return await _fetch_tokens_with_jar(jar, storage_path, **await resolve_route(storage_path))

    coordinator = _auth_recovery.ColdRecoveryCoordinator(
        state=_auth_recovery.ColdRecoveryState.process_default(),
        single_flight=_single_flight.SingleFlight.process_default(),
        should_try_refresh=should_try_refresh,
        resolve_refresh_path=resolve_refresh_path,
        run_refresh_attempt=run_refresh_attempt,
        load_cookie_pair=lambda path: _auth_cookies._build_cookie_pair_from_storage(path),
        run_headless_attempt=lambda path, headless: _auth_recovery._try_headless_reauth_result(
            storage_path=path,
            allow_headless=headless,
        ),
        run_master_token_attempt=lambda path: _auth_recovery._try_master_token_reauth_result(
            storage_path=path
        ),
        validate_recovered=validate_recovered,
        fetch_recovered=fetch_recovered,
        replace_cookie_jar=lambda target, source: _replace_cookie_jar(target, source),
        snapshot_cookie_jar=lambda jar: _auth_storage.snapshot_cookie_jar(jar),
        clone_cookie_jar=lambda jar: _auth_cookies._clone_cookie_jar(jar),
    )
    return await coordinator.recover(
        initial_error=err,
        cookie_jar=cookie_jar,
        storage_path=storage_path,
        env_auth=env_auth,
        allow_headless=allow_headless,
        baseline=baseline,
    )


async def _fetch_tokens_with_jar(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    *,
    authuser: int = 0,
    account_email: str | None = None,
    force_authuser_query: bool = False,
    poke: bool = True,
) -> tuple[str, str]:
    """Internal: fetch CSRF and session tokens using a pre-built cookie jar.

    This is the single implementation for all token-fetch paths. All public
    functions (fetch_tokens, fetch_tokens_with_domains) delegate to this.

    Before fetching tokens, makes a best-effort POST to accounts.google.com to
    rotate __Secure-1PSIDTS; see ``_poke_session``. The poke may be skipped if
    ``storage_path`` was modified within the rate-limit window — that path
    relies on the existing on-disk cookies still being fresh.

    Args:
        cookie_jar: httpx.Cookies jar with auth cookies (domain-preserving or fallback).
        storage_path: Optional storage_state.json path, forwarded to
            ``_poke_session`` to gate the rotation poke.
        authuser: Google account index to authenticate as. ``0`` is the
            default account.
        account_email: Stable account email to use instead of the integer
            index when known.
        force_authuser_query: Append ``?authuser=0`` when callers explicitly
            requested account index 0. Implicit default-account calls leave the
            URL byte-identical to pre-multi-account behavior.
        poke: When ``False``, skip the layer-1 ``_poke_session`` rotation POST
            entirely. The passive read-only validation path
            (:func:`fetch_tokens_passive`) sets this so a readiness probe does
            not rotate ``__Secure-1PSIDTS`` server-side without persisting the
            result (which would stale the on-disk jar).

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    logger.debug("Fetching CSRF and session tokens from NotebookLM")

    from .._curl_cffi_transport import resolve_transport_factory

    async with resolve_transport_factory()(cookies=cookie_jar) as client:
        if poke:
            await _poke_session(client, storage_path)

        url = f"{get_base_url()}/"
        if account_email or authuser or force_authuser_query:
            url = f"{url}?{authuser_query(authuser, account_email)}"
        response = await client.get(
            url,
            follow_redirects=True,
            timeout=30.0,
        )
        response.raise_for_status()

        final_url = str(response.url)
        # Redirect hops, in order. Google's ``/CookieMismatch`` interstitial 302s
        # onward to a support.google.com help article, so it is only ever visible
        # here — never in ``final_url``. (The curl_cffi transport rebuilds a
        # synthetic response and reports an empty history; that path simply
        # loses this one classification rather than misreporting it.)
        redirect_urls = tuple(str(hop.url) for hop in response.history)

        # Classify everything decidable from the URLs BEFORE touching the body.
        # This is not an optimisation: Google's sign-in page carries its own
        # ``WIZ_global_data`` with its own ``SNlM0e``/``FdrFJe`` (live-captured;
        # see ``_url_only_extraction_failure`` for the exact request), and the
        # extractors never check which host answered — so handing a sign-in page
        # to ``extract_csrf_from_html`` returns *that* page's token instead of
        # raising. Delegating to the shared classifier (instead of
        # re-implementing the checks here) keeps the gate -> cookie-mismatch ->
        # auth-redirect precedence identical on both paths; hand-rolling it here
        # is exactly how this path once let a mismatch hop preempt the #1630
        # region gate (#2038).
        url_failure = _url_only_extraction_failure(final_url, redirect_urls)
        if url_failure is not None:
            raise url_failure

        csrf = extract_csrf_from_html(response.text, final_url, redirect_urls=redirect_urls)
        session_id = extract_session_id_from_html(
            response.text, final_url, redirect_urls=redirect_urls
        )

        # httpx copies the input Cookies object into the client. Copy any
        # redirect Set-Cookie updates back to the caller's jar before it is
        # persisted.
        _replace_cookie_jar(cookie_jar, client.cookies)

        logger.debug("Authentication tokens obtained successfully")
        return csrf, session_id


async def fetch_tokens(
    cookies: _auth_cookies.CookieInput,
    storage_path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
) -> tuple[str, str]:
    """Fetch tokens from a cookie mapping. For backward compatibility.

    Prefer NotebookLMClient.from_storage(), which preserves cookie domains. If
    ``NOTEBOOKLM_REFRESH_CMD`` is set and auth has expired, the command is run
    with ``shell=False`` by default (or via the platform shell when
    ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1``), cookies are reloaded from
    ``storage_path`` or the active profile storage path, and token fetch is
    retried once. Refresh commands receive ``NOTEBOOKLM_REFRESH_STORAGE_PATH``
    and ``NOTEBOOKLM_REFRESH_PROFILE`` in their environment.

    Args:
        cookies: Google auth cookies. Mutated in place on refresh.
        storage_path: Optional storage_state.json path to reload after refresh.
        profile: Optional profile name exposed to the refresh command.
        authuser: Optional explicit Google account index. Defaults to the
            persisted profile value, or 0 when none exists.
        account_email: Optional explicit Google account email. When provided,
            it is used as the auth routing value instead of the integer index.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
        RuntimeError: If ``NOTEBOOKLM_REFRESH_CMD`` is set but fails
    """
    jar = await asyncio.to_thread(build_cookie_jar, cookies=cookies, storage_path=storage_path)
    csrf, session_id, refreshed, _post_refresh_snapshot = await _fetch_tokens_with_refresh(
        jar,
        storage_path,
        profile,
        authuser=authuser,
        account_email=account_email,
        force_authuser_query=authuser is not None,
    )
    if refreshed:
        fresh = _cookie_map_from_jar(jar)
        _update_cookie_input(cookies, fresh)
    return csrf, session_id


def _merge_domain_fetch_observation(
    store: ProfileStore,
    observation: CookieJar,
    baseline: CookieJar,
) -> CookieJar:
    result = store.merge_cookie_observation(observation, baseline=baseline)
    if not result.advances_ordering:
        return baseline
    next_baseline = result.next_baseline
    if next_baseline is None:  # pragma: no cover - result invariant
        raise AssertionError("advancing refresh merge must provide a baseline")
    return next_baseline


async def fetch_tokens_with_domains(
    path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
    allow_headless: bool = False,
) -> tuple[str, str]:
    """Fetch tokens with domain-preserving cookies and persist their observation."""
    storage_path = _auth_cookies.resolve_auth_storage_path(path, profile)
    pair = await asyncio.to_thread(_auth_cookies._build_cookie_pair_from_storage, storage_path)
    live = pair.live

    async def resolve_route(route_path: Path | None) -> dict[str, Any]:
        return _resolve_token_route_kwargs(
            route_path,
            authuser=authuser,
            account_email=account_email,
        )

    csrf, session_id, selected_baseline = await _fetch_tokens_with_exact_baseline(
        live,
        storage_path,
        profile,
        initial_baseline=pair.baseline,
        resolve_route=resolve_route,
        allow_headless=allow_headless,
        env_auth=storage_path is None,
    )
    if storage_path is None:
        logger.debug("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var")
        return csrf, session_id
    store = ProfileStore(storage_path)
    observation = CookieJar.from_httpx(live)
    selected_baseline = await asyncio.to_thread(
        _merge_domain_fetch_observation,
        store,
        observation,
        selected_baseline,
    )
    return csrf, session_id


async def fetch_tokens_passive(
    path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
) -> tuple[str, str]:
    """Validate the auth cookies on disk without any side effects.

    Performs the same token-fetch round-trip (a homepage GET that re-mints
    CSRF / session) as :func:`fetch_tokens_with_domains`, but is strictly
    read-only. Unlike that function it:

    * never runs ``NOTEBOOKLM_REFRESH_CMD`` (no re-auth hook / subprocess);
    * never fires the layer-1 keepalive poke that rotates ``__Secure-1PSIDTS``
      server-side;
    * never fires the inline ``RotateCookies`` PSIDTS recovery (it loads the jar
      with the no-recovery strict loader, so a missing/expired PSIDTS surfaces as
      a plain ``ValueError`` instead of a network POST + disk write); and
    * never writes rotated cookies back to ``storage_state.json``.

    This is the readiness probe an unattended monitor (systemd / cron health
    check) wants: it answers "do the cookies currently on disk authenticate?"
    without mutating state, spawning a refresh subprocess, or racing concurrent
    real work (issue #1569). Pair it with a passive ``auth check --test`` or
    ``auth refresh --verify``.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.
        profile: Optional profile name used to resolve the storage path.
        authuser: Optional explicit Google account index. Defaults to the
            persisted profile value, or 0 when none exists.
        account_email: Optional explicit Google account email. When provided,
            it is used as the auth routing value instead of the integer index.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        httpx.HTTPError: If request fails.
        ValueError: If tokens cannot be extracted (e.g. redirected to sign-in).
    """
    path = _auth_cookies.resolve_auth_storage_path(path, profile)
    # Strict (no-recovery) loader: a missing/expired PSIDTS raises ``ValueError``
    # rather than triggering the inline ``RotateCookies`` rotation + save. A
    # readiness probe reports "not authenticated"; it does not heal. Offloaded.
    jar = await asyncio.to_thread(_build_httpx_cookies_from_storage_strict, path)
    route_kwargs = _resolve_token_route_kwargs(path, authuser=authuser, account_email=account_email)
    # poke=False + no save_cookies_to_storage ⇒ zero side effects on disk or
    # on the server-side cookie rotation state.
    return await _fetch_tokens_with_jar(jar, path, poke=False, **route_kwargs)
