"""Refresh-cmd coordination + token-fetch entry points for authentication.

This private module owns the ``NOTEBOOKLM_REFRESH_CMD`` subprocess flow, the
per-loop coalescing of refresh attempts, and the public ``fetch_tokens`` /
``fetch_tokens_with_domains`` entry points. ``notebooklm.auth`` re-exports
every name listed here; the facade write-through in ``_AuthFacadeModule``
mirrors any monkeypatched values back to this module so the existing white-box
tests (``monkeypatch.setattr(auth_mod, "_run_refresh_cmd", ...)``,
``..., "snapshot_cookie_jar"``, etc.) keep working after the move.

Logger name is pinned to ``"notebooklm.auth"`` (NOT ``__name__``) so existing
``caplog`` assertions targeting ``notebooklm.auth`` keep matching the records
emitted from the moved bodies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import threading
import weakref
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from .._env import get_base_url
from .._url_utils import is_google_auth_redirect
from ..paths import get_storage_path, resolve_profile
from . import cookies as _auth_cookies
from . import extraction as _auth_extraction
from . import headers as _auth_headers
from . import keepalive as _keepalive
from . import paths as _auth_paths
from . import storage as _auth_storage
from .account import authuser_query

logger = logging.getLogger("notebooklm.auth")

# --- Names aliased from sibling modules --------------------------------------
# The moved bodies historically resolved these names against ``notebooklm.auth``
# (where they were direct-assigned). Re-aliasing them here gives each one a
# rebindable bare name local to this module; the ``notebooklm.auth`` facade
# write-through (``_AuthFacadeModule._REFRESH_DEP_MIRROR_NAMES``) mirrors any
# monkeypatched value back to this module so the bare-name lookups inside the
# moved bodies still observe the patch.
build_httpx_cookies_from_storage = _auth_cookies.build_httpx_cookies_from_storage
_replace_cookie_jar = _auth_cookies._replace_cookie_jar
_cookie_map_from_jar = _auth_cookies._cookie_map_from_jar
build_cookie_jar = _auth_cookies.build_cookie_jar
flatten_cookie_map = _auth_cookies.flatten_cookie_map
_update_cookie_input = _auth_cookies._update_cookie_input
snapshot_cookie_jar = _auth_storage.snapshot_cookie_jar
save_cookies_to_storage = _auth_storage.save_cookies_to_storage
extract_csrf_from_html = _auth_extraction.extract_csrf_from_html
extract_session_id_from_html = _auth_extraction.extract_session_id_from_html
_safe_url = _auth_extraction._safe_url
_resolve_token_route_kwargs = _auth_headers._resolve_token_route_kwargs

# Env-var names live in ``_auth.paths``; aliased so the refresh bodies can
# reference them without an extra hop.
NOTEBOOKLM_REFRESH_CMD_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_ENV
NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV = _auth_paths.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV
_REFRESH_ATTEMPTED_ENV = _auth_paths._REFRESH_ATTEMPTED_ENV

# ``_poke_session`` lives in ``_auth.keepalive``; aliased here as a rebindable
# bare name so monkeypatches to ``notebooklm.auth._poke_session`` flow through
# the facade write-through into this module's slot.
_poke_session = _keepalive._poke_session


# --- Refresh-coalescing state ------------------------------------------------
# The ContextVar prevents same-task retry loops in the parent process. The env
# flag is passed only to child refresh commands so recursive CLI calls skip refresh.
_REFRESH_ATTEMPTED_CONTEXT: ContextVar[bool] = ContextVar(
    "_REFRESH_ATTEMPTED_CONTEXT", default=False
)
# In-process state for refresh coordination, keyed per resolved storage path.
#
# Two layers of protection are required:
#
# - ``_REFRESH_STATE_LOCK`` (sync ``threading.Lock``) makes the
#   ``_REFRESH_GENERATIONS`` check-and-update atomic ACROSS event loops.
#   Two loops sharing a storage path each hold their own ``asyncio.Lock``
#   (see below), so the asyncio lock alone cannot serialize the generation
#   bump.
#
# - ``_REFRESH_LOCKS_BY_LOOP`` mirrors the keepalive ``_POKE_LOCKS_BY_LOOP``
#   pattern: ``asyncio.Lock`` is loop-bound, so a per-loop / per-resolved-
#   storage-path registry avoids the cross-loop / cross-thread hazard of a
#   module-global ``asyncio.Lock`` that binds to the first event loop that
#   uses it. The outer ``WeakKeyDictionary`` is keyed on the loop object so
#   the inner dict is reclaimed when the loop is garbage-collected.
_REFRESH_STATE_LOCK = threading.Lock()
_REFRESH_LOCKS_BY_LOOP: weakref.WeakKeyDictionary[Any, dict[Path | None, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)
_REFRESH_GENERATIONS: dict[str, int] = {}

# In-flight ``asyncio.Future`` registry for refresh-cmd coalescing.
#
# Same-loop concurrent callers that both encounter auth-expiry coalesce on a
# single in-flight subprocess by sharing a per-resolved-storage-path
# ``asyncio.Future``. The future is keyed per-loop because ``asyncio.Future``
# is loop-bound; cross-loop coordination falls back to the
# ``_REFRESH_GENERATIONS`` counter guarded by ``_REFRESH_STATE_LOCK``.
#
# The strong-ref ``_REFRESH_INFLIGHT_TASKS`` set keeps the shielded subprocess
# Tasks alive so the asyncio GC does not collect them. The task self-removes
# via ``add_done_callback(set.discard)`` once settled.
_REFRESH_INFLIGHT_BY_LOOP: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Future[None]]] = (
    weakref.WeakKeyDictionary()
)
# Strong-ref set keyed by task identity. ``set.add`` / ``set.discard`` are
# atomic under CPython's GIL (individual bytecode mutations on the underlying
# hash table cannot interleave), so concurrent ``add`` / ``discard`` calls
# from different event-loop threads are safe without an explicit lock. This is
# implementation-specific to CPython; non-CPython runtimes would need a
# ``threading.Lock`` here.
_REFRESH_INFLIGHT_TASKS: set[asyncio.Task[None]] = set()


def _get_inflight_registry() -> dict[str, asyncio.Future[None]]:
    """Return the per-loop in-flight refresh-cmd future registry.

    Mirrors ``_get_refresh_lock``: ``asyncio.Future`` is loop-bound, so we
    need a per-loop registry. ``_REFRESH_STATE_LOCK`` makes the lookup /
    insert atomic across threads (different loops on different threads can
    each populate their own per-loop dict concurrently).
    """
    loop = asyncio.get_running_loop()
    with _REFRESH_STATE_LOCK:
        per_loop = _REFRESH_INFLIGHT_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _REFRESH_INFLIGHT_BY_LOOP[loop] = per_loop
        return per_loop


async def _coalesced_run_refresh_cmd(
    refresh_key: str,
    resolved_storage_path: Path,
    profile: str | None,
) -> None:
    """Run ``_run_refresh_cmd`` once per ``refresh_key`` on this event loop.

    Same-loop concurrent callers that hit this function while a refresh is
    in flight will await the same underlying ``asyncio.Future`` rather than
    spawning their own subprocess.

    Cancel-safety design:

    - The subprocess is driven by a strongly-referenced background
      ``asyncio.Task`` (registered in ``_REFRESH_INFLIGHT_TASKS``) so it
      survives cancellation of any individual awaiter.
    - Each awaiter wraps the future in ``asyncio.shield`` so local
      cancellation of the awaiter does NOT cancel the shared subprocess —
      mirrors the ``ClientCore._await_refresh`` pattern used for the RPC
      refresh path.
    - The caller in ``_fetch_tokens_with_refresh`` keeps re-awaiting the
      shielded future under the per-loop asyncio lock so the lock is not
      released until the subprocess settles. This prevents a duplicate
      subprocess from being spawned if the lock is released mid-refresh
      and a second caller observes a partially-completed state.
    """
    loop = asyncio.get_running_loop()
    registry = _get_inflight_registry()
    with _REFRESH_STATE_LOCK:
        existing = registry.get(refresh_key)
        leader = existing is None or existing.done()
        if leader:
            future: asyncio.Future[None] = loop.create_future()
            registry[refresh_key] = future
        else:
            future = existing  # type: ignore[assignment]

    if leader:
        task = asyncio.create_task(_run_refresh_cmd(resolved_storage_path, profile))
        # Strong-ref pattern: without ``add_done_callback`` the task can be
        # collected by the asyncio GC before completion if no awaiter is
        # holding a reference.
        _REFRESH_INFLIGHT_TASKS.add(task)
        task.add_done_callback(_REFRESH_INFLIGHT_TASKS.discard)

        def _settle(t: asyncio.Task[None]) -> None:
            # ``Future.set_*`` is loop-affine; the callback runs on the owning
            # loop (same loop that created the future and the task), so direct
            # ``set_result`` / ``set_exception`` is safe.
            if not future.done():
                if t.cancelled():
                    future.cancel()
                else:
                    exc = t.exception()
                    if exc is not None:
                        future.set_exception(exc)
                    else:
                        future.set_result(None)
            # Intentionally LEAVE the (now-done) future in the registry so the
            # caller's CancelledError handler in ``_fetch_tokens_with_refresh``
            # can still inspect ``inflight.exception()`` after a cancel/settle
            # race (CodeRabbit PR #621 finding). The leader-check at the
            # get-or-create site (``existing is None or existing.done()``)
            # treats a done future as overwritable, so the next refresh
            # cycle's leader replaces this slot — no accumulation.

        task.add_done_callback(_settle)

    # All callers (leader + followers) await the shared future under shield.
    # Re-raises subprocess exception to every awaiter.
    await asyncio.shield(future)


def _get_refresh_lock(resolved_storage_path: Path | None) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``(running event loop, resolved storage path)``.

    Mirrors ``_get_poke_lock``. Keyed on the RESOLVED storage path so callers
    passing ``(None, profile="foo")`` share the lock with callers passing the
    explicit profile-resolved path.
    """
    loop = asyncio.get_running_loop()
    with _REFRESH_STATE_LOCK:
        per_loop = _REFRESH_LOCKS_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _REFRESH_LOCKS_BY_LOOP[loop] = per_loop
        lock = per_loop.get(resolved_storage_path)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[resolved_storage_path] = lock
        return lock


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

    Raises:
        RuntimeError: If the refresh command is missing, parses to an empty
            argv, is malformed (unterminated quote), times out, or exits
            non-zero.
    """
    cmd = os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV)
    if not cmd:
        raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} is not set; cannot refresh cookies.")
    refresh_env = os.environ.copy()
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
        # P1-18: do NOT interpolate stdout/stderr into the user-facing raise.
        # Subprocesses commonly print bearer tokens, cookies, and absolute
        # paths into a user's credentials directory. ``RuntimeError`` bubbles
        # up through ``cli.error_handler`` and lands on stderr (or a JSON
        # envelope), which is the wrong audience for that material.
        #
        # Two-channel disclosure: the user sees only exit code + executable
        # basename; developers running with ``-vv`` get the full output
        # through the package's redacting DEBUG logger.
        # Claude bot review feedback: in shell-mode ``run_target`` is the raw
        # command STRING, not a list. Extract the basename of its first token
        # so users still see a useful script name (the string is user-supplied
        # and not a secret — its argv[0] equivalent is safe to surface).
        if isinstance(run_target, list) and run_target:
            executable_basename = os.path.basename(run_target[0])
        elif isinstance(run_target, str) and run_target.strip():
            executable_basename = os.path.basename(run_target.split()[0])
        else:
            executable_basename = "shell"
        logger.debug(
            "%s exited %d. stdout=%r stderr=%r",
            NOTEBOOKLM_REFRESH_CMD_ENV,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        raise RuntimeError(
            f"{NOTEBOOKLM_REFRESH_CMD_ENV} exited {result.returncode} "
            f"(executable: {executable_basename}). "
            f"Run with --verbose to see captured stdout/stderr in the debug log."
        )
    logger.info("NotebookLM cookies refreshed via %s", NOTEBOOKLM_REFRESH_CMD_ENV)


async def _fetch_tokens_with_refresh(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int = 0,
    account_email: str | None = None,
    force_authuser_query: bool = False,
) -> tuple[str, str, bool, _auth_storage.CookieSnapshot | None]:
    """Fetch tokens, optionally running NOTEBOOKLM_REFRESH_CMD on auth expiry.

    Returns ``(csrf, session_id, refreshed, post_refresh_snapshot)``.

    When ``refreshed`` is ``True``, ``post_refresh_snapshot`` is a snapshot
    captured **immediately after** ``_replace_cookie_jar`` swaps in the
    refresh-cmd output and **before** the retry token fetch can mutate the
    jar with redirect Set-Cookies. Callers must use that snapshot as the
    save baseline; re-snapshotting the jar after this function returns
    would include the retry's rotations in the baseline (so they would
    never reach disk on the subsequent save).

    When ``refreshed`` is ``False`` the snapshot is ``None`` (no refresh
    happened; caller's pre-fetch snapshot is still the right baseline).
    """
    try:
        route_kwargs: dict[str, Any] = {"authuser": authuser}
        if account_email is not None:
            route_kwargs["account_email"] = account_email
        if force_authuser_query:
            route_kwargs["force_authuser_query"] = True
        csrf, session_id = await _fetch_tokens_with_jar(cookie_jar, storage_path, **route_kwargs)
        return csrf, session_id, False, None
    except ValueError as err:
        if not _should_try_refresh(err):
            raise
        logger.warning(
            "NotebookLM auth failed (%s). Running %s to refresh cookies.",
            err,
            NOTEBOOKLM_REFRESH_CMD_ENV,
        )
        # Canonicalize the storage path so different representations of the
        # same physical file (relative vs absolute, with or without symlinks,
        # ``~`` shorthand) hash to the same lock-registry / generation key.
        # ``get_storage_path`` already returns a resolved path, but a
        # caller-supplied ``storage_path`` may be relative or a symlink.
        refresh_storage_path = (
            (storage_path or get_storage_path(profile=profile)).expanduser().resolve()
        )
        refresh_key = str(refresh_storage_path)
        # Snapshot the generation BEFORE acquiring the async lock so we can
        # detect whether a concurrent refresh (potentially on a different
        # event loop) bumped it while we were waiting. ``_REFRESH_STATE_LOCK``
        # makes this read atomic with the later check-and-update below.
        with _REFRESH_STATE_LOCK:
            refresh_generation = _REFRESH_GENERATIONS.get(refresh_key, 0)
        refresh_token = _REFRESH_ATTEMPTED_CONTEXT.set(True)
        try:
            async with _get_refresh_lock(refresh_storage_path):
                # Bump generation ONLY after the current-attempt subprocess
                # succeeds — never eagerly. An earlier implementation bumped
                # the generation BEFORE ``_run_refresh_cmd``; when the
                # subprocess failed, the phantom bump made concurrent waiters
                # short-circuit and proceed with stale storage. The bump
                # itself happens just below, immediately before
                # ``build_httpx_cookies_from_storage`` reloads the freshly-
                # written disk state.
                #
                # Re-check under the sync state lock so the read is atomic
                # ACROSS event loops. The per-loop asyncio lock only
                # serializes within a single loop; a second loop sharing this
                # storage path holds its own asyncio.Lock.
                with _REFRESH_STATE_LOCK:
                    current_generation = _REFRESH_GENERATIONS.get(refresh_key, 0)
                    # ``current > refresh_generation`` means another caller
                    # (any loop) has SUCCESSFULLY refreshed since we observed
                    # auth-expiry — we can skip ``_run_refresh_cmd`` and just
                    # reload the freshly-written storage.
                    should_run_refresh = current_generation <= refresh_generation
                if should_run_refresh:
                    # Cancel-safety: drive the subprocess through the shared
                    # in-flight future. Same-loop concurrent callers coalesce
                    # on the same subprocess. If THIS caller is cancelled
                    # while the subprocess is in flight, we keep awaiting the
                    # shielded future so the asyncio lock is NOT released
                    # until the subprocess settles — otherwise a second
                    # caller could spawn a duplicate concurrent refresh by
                    # observing the mid-flight lock release.
                    caller_cancelled = False
                    # ``observed_inflight`` distinguishes "current-attempt
                    # subprocess actually ran" from "cancellation arrived
                    # before any subprocess registered for THIS attempt"
                    # (issue #816). Only when we have proof of the current
                    # attempt do we have license to bump the generation.
                    observed_inflight = False
                    subprocess_exc: BaseException | None = None
                    # Snapshot the inflight registry slot BEFORE entering
                    # the await. The ``_settle`` callback intentionally
                    # leaves done futures in the registry so the
                    # cancel/settle race fix from CodeRabbit PR #621 can
                    # still inspect ``inflight.exception()``; that
                    # retention also means the registry may still hold a
                    # STALE done future from a previous refresh cycle when
                    # our await starts. We distinguish that stale slot
                    # from a current-attempt future so the
                    # cancel-before-register narrow window (issue #816)
                    # does not attribute a prior cycle's success to this
                    # caller's no-op attempt.
                    registry = _get_inflight_registry()
                    with _REFRESH_STATE_LOCK:
                        prior_inflight = registry.get(refresh_key)
                    # A pre-existing registry entry that was already done
                    # at capture time is stale leftover from a prior cycle.
                    # A pre-existing entry that was still active is a
                    # sibling leader's current-cycle future — same-loop
                    # coalescing means we legitimately follow it.
                    prior_was_active = prior_inflight is not None and not prior_inflight.done()
                    while True:
                        try:
                            await _coalesced_run_refresh_cmd(
                                refresh_key, refresh_storage_path, profile
                            )
                            # Normal return only happens when the shielded
                            # future resolved with success — i.e. a
                            # current-attempt subprocess ran to completion.
                            observed_inflight = True
                            break
                        except asyncio.CancelledError:
                            # Caller-side cancellation. Re-enter the await
                            # so the shielded subprocess can settle while we
                            # still hold the asyncio lock.
                            caller_cancelled = True
                            with _REFRESH_STATE_LOCK:
                                inflight = registry.get(refresh_key)
                            # Determine whether the registry slot belongs
                            # to the CURRENT refresh attempt. Two cases
                            # mean "yes":
                            #   (a) the slot was overwritten during our
                            #       await (``inflight is not prior_inflight``
                            #       — a new leader inserted a fresh future);
                            #   (b) the slot already held an actively-
                            #       running sibling leader at capture time
                            #       (``prior_was_active``) — same-loop
                            #       coalescing means we legitimately follow
                            #       its subprocess.
                            # Otherwise the slot is either empty or a
                            # stale done future that ``_settle``
                            # intentionally left behind from a prior cycle
                            # (PR #621 cancel/settle race retention).
                            # Treating that stale entry as proof of our
                            # attempt would re-bump generation against an
                            # outdated result — the warm-registry variant
                            # of #816.
                            if inflight is None or (
                                inflight is prior_inflight and not prior_was_active
                            ):
                                # No current-attempt future to wait on
                                # (issue #816 narrow window: cancellation
                                # arrived before the registry insert).
                                break
                            observed_inflight = True
                            if inflight.done():
                                # Current attempt already settled and we
                                # absorbed the cancellation. ``_settle``
                                # left the done future in the registry so
                                # this branch can inspect its terminal
                                # state (PR #621 cancel/settle race).
                                if inflight.cancelled():
                                    # Subprocess itself was cancelled —
                                    # treat as failure (do not bump gen).
                                    subprocess_exc = asyncio.CancelledError()
                                else:
                                    subprocess_exc = inflight.exception()
                                break
                            # Otherwise (current-attempt future still in
                            # flight) loop and re-await the shielded
                            # future so the asyncio lock is not released
                            # until the subprocess settles.
                        except BaseException as exc:  # noqa: BLE001
                            subprocess_exc = exc
                            break

                    if subprocess_exc is not None:
                        # Subprocess failed — DO NOT bump generation.
                        # Concurrent / subsequent waiters re-attempt the
                        # refresh instead of short-circuiting on a phantom
                        # bump.
                        if caller_cancelled:
                            # Caller cancellation takes priority for THIS
                            # caller.
                            raise asyncio.CancelledError() from subprocess_exc
                        raise subprocess_exc

                    if observed_inflight:
                        # Subprocess succeeded AND we're about to reload
                        # storage. Bump the generation now so other callers
                        # (any loop) see the success and skip their own
                        # subprocess. The bump is atomic across loops via
                        # ``_REFRESH_STATE_LOCK``.
                        with _REFRESH_STATE_LOCK:
                            # ``max(...)`` defends against the rare
                            # interleaving where another loop's pre-lock
                            # capture was AFTER ours and bumped past us.
                            existing = _REFRESH_GENERATIONS.get(refresh_key, 0)
                            _REFRESH_GENERATIONS[refresh_key] = max(
                                existing, refresh_generation + 1
                            )

                    if caller_cancelled:
                        # Generation handling depends on whether a subprocess
                        # actually ran:
                        # * observed_inflight=True: subprocess succeeded (the
                        #   failure branch already raised above); the bump
                        #   above persists for other callers' benefit.
                        # * observed_inflight=False: no subprocess ever
                        #   registered, so generation stays at the pre-fetch
                        #   baseline (issue #816) — concurrent / subsequent
                        #   waiters re-attempt the refresh instead of
                        #   short-circuiting on a phantom bump.
                        # Either way THIS caller propagates cancellation
                        # rather than completing the retry.
                        raise asyncio.CancelledError()
                fresh_jar = build_httpx_cookies_from_storage(refresh_storage_path)
                _replace_cookie_jar(cookie_jar, fresh_jar)
                # Capture the baseline NOW — after the wholesale replacement
                # but before the retry fetch can mutate the jar.
                post_refresh_snapshot = snapshot_cookie_jar(cookie_jar)
            route_kwargs = {"authuser": authuser}
            if account_email is not None:
                route_kwargs["account_email"] = account_email
            if force_authuser_query:
                route_kwargs["force_authuser_query"] = True
            csrf, session_id = await _fetch_tokens_with_jar(
                cookie_jar, refresh_storage_path, **route_kwargs
            )
            return csrf, session_id, True, post_refresh_snapshot
        finally:
            _REFRESH_ATTEMPTED_CONTEXT.reset(refresh_token)


async def _fetch_tokens_with_jar(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    *,
    authuser: int = 0,
    account_email: str | None = None,
    force_authuser_query: bool = False,
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

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    logger.debug("Fetching CSRF and session tokens from NotebookLM")

    async with httpx.AsyncClient(cookies=cookie_jar) as client:
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

        # Check if we were redirected to login
        if is_google_auth_redirect(final_url):
            raise ValueError(
                "Authentication expired or invalid. "
                "Redirected to: " + _safe_url(final_url) + "\n"
                "Run 'notebooklm login' to re-authenticate."
            )

        csrf = extract_csrf_from_html(response.text, final_url)
        session_id = extract_session_id_from_html(response.text, final_url)

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

    Prefer AuthTokens.from_storage() which preserves cookie domains. If
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
    jar = build_cookie_jar(cookies=cookies, storage_path=storage_path)
    route_kwargs = _resolve_token_route_kwargs(
        storage_path,
        authuser=authuser,
        account_email=account_email,
    )
    csrf, session_id, refreshed, _post_refresh_snapshot = await _fetch_tokens_with_refresh(
        jar, storage_path, profile, **route_kwargs
    )
    if refreshed:
        fresh = _cookie_map_from_jar(jar)
        _update_cookie_input(cookies, fresh)
    return csrf, session_id


async def fetch_tokens_with_domains(
    path: Path | None = None,
    profile: str | None = None,
    *,
    authuser: int | None = None,
    account_email: str | None = None,
) -> tuple[str, str]:
    """Fetch tokens with domain-preserving cookies from storage.

    Used by CLI helpers. Loads storage, builds jar, fetches tokens, optionally
    runs NOTEBOOKLM_REFRESH_CMD on auth expiry, and persists any refreshed
    cookies back.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.
        profile: Optional profile name exposed to the refresh command.
        authuser: Optional explicit Google account index. Defaults to the
            persisted profile value, or 0 when none exists.
        account_email: Optional explicit Google account email. When provided,
            it is used as the auth routing value instead of the integer index.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        httpx.HTTPError: If request fails.
        ValueError: If tokens cannot be extracted from response.
        RuntimeError: If ``NOTEBOOKLM_REFRESH_CMD`` is set but fails.
    """
    if path is None and (profile is not None or "NOTEBOOKLM_AUTH_JSON" not in os.environ):
        path = get_storage_path(profile=profile)
    jar = build_httpx_cookies_from_storage(path)
    route_kwargs = _resolve_token_route_kwargs(path, authuser=authuser, account_email=account_email)
    # Capture the open-time snapshot before any rotation could fire. The
    # snapshot is the input to the dirty-flag/delta merge that closes the
    # stale-overwrite-fresh race (docs/auth-keepalive.md §3.4.1).
    snapshot = snapshot_cookie_jar(jar)
    csrf, session_id, refreshed, post_refresh_snapshot = await _fetch_tokens_with_refresh(
        jar, path, profile, **route_kwargs
    )
    if refreshed and post_refresh_snapshot is not None:
        # NOTEBOOKLM_REFRESH_CMD replaced the jar wholesale. Use the snapshot
        # captured immediately after the replacement (before the retry fetch
        # added redirect Set-Cookies); re-snapshotting here would let those
        # retry rotations be absorbed into the baseline and never reach disk.
        snapshot = post_refresh_snapshot
    # Offload the blocking storage save to a worker thread so the
    # atomic-replace + fsync + flock can't stall the event loop on
    # slow filesystems.
    await asyncio.to_thread(save_cookies_to_storage, jar, path, original_snapshot=snapshot)
    return csrf, session_id
