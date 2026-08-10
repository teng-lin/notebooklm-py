"""Master-token headless auth: mint NotebookLM web cookies from a durable Google
``aas_et/`` master token, with no per-session browser.

Flow (proven against the live API in the #1638 spike):

    oauth_token (single-use, from one EmbeddedSetup browser sign-in)
      --exchange_token-->  aas_et/ master token   (durable; persisted 0600)
      --perform_oauth-->   ya29 OAuthLogin token
      --OAuthLogin?issueuberauth=1-->  uberauth
      --MergeSession-->    SID/SAPISID/__Secure-1PSID/... cookie jar

The minted jar authorizes the existing web client (batchexecute, upload,
download). After MergeSession the mint also fires one ``RotateCookies`` POST to
add ``__Secure-1PSIDTS`` (the rotating freshness partner of ``__Secure-1PSID``),
so the stored jar is complete at rest. That POST is best-effort — if Google
withholds it, the standard inline recovery still mints it on first load from
``SID`` + ``APISID``/``SAPISID`` (secondary binding).

SECURITY: the master token is full-account, durable, infostealer-grade — use a
dedicated/throwaway account only. Never log the oauth_token, master token, ya29,
uberauth, or cookie values.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

from .master_token_bootstrap import BootstrapOutcome, MasterTokenBootstrapper, _BootstrapError

# The bootstrap lock's PATH is derived by the one shared credential-lock
# derivation in ``paths.py`` (ADR-0033 PR 1.3) — this module used to hand-roll
# its own ``expanduser().resolve()`` + f-string sibling, the fourth and last
# spelling of a computation whose filenames must never drift apart. Its
# MECHANISM stays ``filelock.FileLock`` here, unchanged and deliberately not
# unified with ``storage._file_lock`` (plan §1/§5: a cross-version and
# cross-platform interop event this effort does not take). ``paths`` imports
# nothing from this package, so this is a plain module-level import, not one of
# the deferred cycle-breaks below.
from .master_token_file import MasterTokenFile
from .master_token_types import MasterToken, MasterTokenError, _MasterTokenRecordError
from .mint_service import MintService, _MintError
from .paths import _bootstrap_lock_path
from .profile_store import ProfileStore

_MASTER_TOKEN_VERSION = 1


def generate_android_id() -> str:
    """Random stable 64-bit hex Android id, generated once per install and
    persisted with the token. Changing it can re-trip Google's new-device risk
    signal on re-mint, so callers must reuse the stored value."""
    return secrets.token_hex(8)


def exchange_master_token(email: str, oauth_token: str, android_id: str) -> str:
    """One-time: a single-use EmbeddedSetup ``oauth_token`` -> durable ``aas_et/``
    master token. Raises :class:`MasterTokenError` on rejection (no secret leak)."""
    caller_exception = sys.exc_info()[1]
    try:
        token = MintService().exchange(email, oauth_token, android_id)
    except _MintError as exc:
        error_context = exc.__context__
        if exc.__cause__ is None and error_context is caller_exception:
            error_context = None
        error_snapshot = (
            str(exc),
            exc.__cause__,
            error_context,
            exc.__suppress_context__,
        )
    else:
        return token.secret
    caller_exception = None
    error = MasterTokenError(error_snapshot[0])
    try:
        raise error
    except MasterTokenError:
        # Raising while the caller handles another exception overwrites
        # ``__context__``. Restore the lower-layer projection after that first
        # raise, then preserve it with a bare re-raise.
        error.__cause__ = error_snapshot[1]
        error.__context__ = error_snapshot[2]
        error.__suppress_context__ = error_snapshot[3]
        raise


async def mint_cookies(email: str, master_token: str, android_id: str) -> httpx.Cookies:
    """Mint a fresh NotebookLM web cookie jar from the master token.

    perform_oauth (sync, offloaded once) -> ya29, then
    OAuthLogin?issueuberauth=1 -> uberauth -> MergeSession -> Set-Cookie jar.
    Raises :class:`MasterTokenError` if the token is revoked or the jar lacks the
    cookies the web client needs.
    """
    caller_exception = sys.exc_info()[1]
    try:
        jar = await MintService().mint(
            MasterToken(email=email, android_id=android_id, secret=master_token)
        )
    except _MintError as exc:
        error_context = exc.__context__
        if exc.__cause__ is None and error_context is caller_exception:
            error_context = None
        error_snapshot = (
            str(exc),
            exc.__cause__,
            error_context,
            exc.__suppress_context__,
        )
    else:
        return jar
    caller_exception = None
    error = MasterTokenError(error_snapshot[0])
    try:
        raise error
    except MasterTokenError:
        # See ``exchange_master_token``: the first raise establishes the
        # traceback; restoring here keeps an active caller exception out of
        # the public chain without retaining the private translation error.
        error.__cause__ = error_snapshot[1]
        error.__context__ = error_snapshot[2]
        error.__suppress_context__ = error_snapshot[3]
        raise


def storage_state_from_jar(jar: httpx.Cookies, *, email: str | None = None) -> dict[str, Any]:
    """Convert a minted jar to a Playwright ``storage_state`` dict the existing
    loader (``build_httpx_cookies_from_storage``) consumes, including the
    ``notebooklm`` account namespace. Reuses ``_cookie_to_storage_state`` so
    secure/httpOnly/expires and ``__Secure-`` prefixes survive (see #365)."""
    from .cookies import _cookie_to_storage_state  # noqa: PLC0415 (avoid import cycle)

    state: dict[str, Any] = {
        "cookies": [_cookie_to_storage_state(c) for c in jar.jar],
        "origins": [],
    }
    if email is not None:
        # Mirrors _auth/storage.write_account_metadata's namespace shape.
        state["notebooklm"] = {"version": 1, "account": {"authuser": 0, "email": email}}
    return state


def persist_minted_jar(
    path: Path,
    jar: httpx.Cookies,
    *,
    email: str | None,
    force: bool = False,
    refuse_unknown_owner: bool = True,
) -> None:
    """Replace the cookies in ``storage_state.json`` with a freshly-minted jar,
    preserving existing CLI context (notebook_id/conversation_id) and refreshing
    the account namespace. Serialized on the shared storage lock so it never
    tears against a running keepalive. Old cookies are *replaced*, not merged —
    a re-mint is a brand-new session.

    Delegates the storage-state write to the canonical
    :func:`notebooklm._auth.storage.persist_minted_jar`, which routes the
    write through ``_atomic_io`` (fsync durability + temp cleanup, closing
    [storage-F5]) under the unified bounded storage lock. This function stays as
    the ``notebooklm.auth``-exported facade symbol.

    Raises :class:`MasterTokenError` (#2103 PR-2 D6) if existing storage belongs
    to a *different* recorded account and ``force`` is not set — the
    authoritative ownership guard, enforced here under the storage-write lock so
    it also covers a caller that mints and persists directly (bypassing
    :func:`bootstrap_from_oauth_token`/:func:`remint_from_stored_token`
    entirely) and closes the TOCTOU window a check-before-mint pre-check alone
    cannot. ``refuse_unknown_owner`` (default ``True``) additionally refuses
    existing storage with NO recorded owner at all; see
    :func:`notebooklm._auth.storage.persist_minted_jar` for why
    ``remint_from_stored_token`` passes ``False`` here."""
    from . import storage  # noqa: PLC0415 (deferred; no cycle either way (verified))

    storage.persist_minted_jar(
        path, jar, email=email, force=force, refuse_unknown_owner=refuse_unknown_owner
    )


# --- master_token.json persistence (mode 0600, beside storage_state.json) ---


def read_master_token(path: Path) -> dict[str, Any] | None:
    """Read a ``master_token.json`` record, or ``None`` if absent. Raises
    :class:`MasterTokenError` on a malformed/old-version file."""
    if not path.exists():
        return None
    token_file = MasterTokenFile(path)
    malformed_message: str | None = None
    try:
        result = token_file._read_present_with_raw()
    except (OSError, json.JSONDecodeError) as exc:
        raise MasterTokenError(f"Unreadable master_token.json: {exc}") from exc
    except _MasterTokenRecordError:
        malformed_message = "master_token.json is malformed or an unsupported version."
    if malformed_message is not None:
        raise MasterTokenError(malformed_message)
    return dict(result.raw)


def write_master_token(path: Path, *, email: str, master_token: str, android_id: str) -> None:
    """Persist a master-token record at mode 0600 (full-account credential).

    Delegates to :func:`notebooklm._auth.storage.write_master_token`,
    which routes the write through ``_atomic_io`` (atomic + fsync-durable + temp
    cleanup) under a bounded sibling lock — closing the lockless-write half of
    [storage-F5]. This function stays as the ``notebooklm.auth``-exported facade
    symbol."""
    from . import storage  # noqa: PLC0415 (deferred; no cycle either way (verified))

    storage.write_master_token(path, email=email, master_token=master_token, android_id=android_id)


# --- the transaction (relocated from cli/services/login/master_token.py,
# #2103 structural follow-up PR-2): the CLI now invokes whole audited
# transactions below, never assembles minting primitives itself. ---


async def _verify_by_listing_notebooks(storage_path: Path) -> int:
    """Smoke-test a minted session: list notebooks. Returns the count.

    This is the ONLY place ``_auth`` reaches up to the top-level client, and
    ADR-0033's PR 0.2 set out to delete the edge by injecting the verifier from
    the call site. Investigation says it is irreducible at this layer, so it is
    documented rather than faked:

    * The sole caller is :func:`bootstrap_from_oauth_token`, which is itself the
      outermost entry point — it is public surface, re-exported as
      ``notebooklm.auth.master_token_bootstrap`` and called by library users
      directly, not only by ``cli/master_token_login.py``.
    * ``verify=True`` is that function's DEFAULT, and the behaviour that default
      names is precisely "open a ``NotebookLMClient`` and list notebooks". So a
      caller-supplied verifier can only remove this import if supplying one
      becomes mandatory — a breaking signature change for every existing
      ``master_token_bootstrap(verify=True)`` call — or if the default body
      moves up into the ``notebooklm.auth`` facade, which would turn an
      identity re-export into a wrapper and change the facade surface (plan §1
      non-goal). Neither is available to a behaviour-frozen mechanical PR.
    * The deferral is load-bearing, and the original ``(avoid import cycle)``
      note is accurate here — unlike the two ``browser_capture`` sites this PR
      corrected. Verified by hoisting it: ``client`` does
      ``from .auth import AuthTokens`` at module scope (``client.py``) and
      ``notebooklm.auth`` imports THIS module, so a top-level import closes
      ``_auth.master_token -> client -> notebooklm.auth -> _auth.master_token``
      and fails at import time with a partially-initialized ``notebooklm.auth``.

    Removing the edge for real belongs with the ADR-0032 facade work that is
    already licensed to reshape ``notebooklm.auth``; it is recorded here so the
    next attempt does not re-derive the same dead end.
    """
    from ..client import NotebookLMClient  # noqa: PLC0415 (cycle via notebooklm.auth)

    async with NotebookLMClient.from_storage(path=str(storage_path)) as client:
        return len(await client.notebooks.list())


def assert_account_writable(*, email: str, storage_path: Path, force: bool = False) -> None:
    """Refuse an advisory cross-account overwrite unless force is set."""
    if force:
        return
    caller_exception = sys.exc_info()[1]
    try:
        try:
            _bootstrapper(storage_path).assert_account_writable(
                email=email,
                force=force,
                session_owner_reader=_session_owner_reader,
            )
        except _BootstrapError as exc:
            error_context = exc.__context__
            if (
                exc.__cause__ is not None
                and exc.__suppress_context__
                and error_context is caller_exception
            ):
                error_context = exc.__cause__
            error_snapshot = (
                str(exc),
                exc.__cause__,
                error_context,
                exc.__suppress_context__,
            )
        else:
            return
        caller_exception = None
        error = MasterTokenError(error_snapshot[0])
        try:
            raise error
        except MasterTokenError:
            error.__cause__ = error_snapshot[1]
            error.__context__ = error_snapshot[2]
            error.__suppress_context__ = error_snapshot[3]
            raise
    finally:
        caller_exception = None


async def bootstrap_from_oauth_token(
    *,
    email: str,
    oauth_token: str,
    storage_path: Path,
    android_id: str | None = None,
    verify: bool = True,
    force: bool = False,
) -> int:
    """Exchange, mint, persist the session then token, and optionally verify."""
    try:
        caller_exception = sys.exc_info()[1]
        try:
            return await _bootstrapper(storage_path).bootstrap_from_oauth_token(
                email=email,
                oauth_token=oauth_token,
                android_id=android_id,
                verify=verify,
                force=force,
                session_owner_reader=_session_owner_reader,
                android_id_generator=_android_id_generator,
            )
        except _BootstrapError as exc:
            error_context = exc.__context__
            if (
                exc.__cause__ is not None
                and exc.__suppress_context__
                and error_context is caller_exception
            ):
                error_context = exc.__cause__
            error_snapshot = (
                str(exc),
                exc.__cause__,
                error_context,
                exc.__suppress_context__,
            )
        caller_exception = None
        error = MasterTokenError(error_snapshot[0])
        try:
            raise error
        except MasterTokenError:
            error.__cause__ = error_snapshot[1]
            error.__context__ = error_snapshot[2]
            error.__suppress_context__ = error_snapshot[3]
            raise
    finally:
        caller_exception = None
        del oauth_token


async def remint_from_stored_token(storage_path: Path) -> httpx.Cookies:
    """Re-mint and strictly reload a session from the paired stored token."""
    caller_exception = sys.exc_info()[1]
    try:
        try:
            return await _bootstrapper(storage_path).remint_from_stored_token(
                strict_loader=_strict_loader
            )
        except _BootstrapError as exc:
            error_context = exc.__context__
            if (
                exc.__cause__ is not None
                and exc.__suppress_context__
                and error_context is caller_exception
            ):
                error_context = exc.__cause__
            error_snapshot = (
                str(exc),
                exc.__cause__,
                error_context,
                exc.__suppress_context__,
            )
        caller_exception = None
        error = MasterTokenError(error_snapshot[0])
        try:
            raise error
        except MasterTokenError:
            error.__cause__ = error_snapshot[1]
            error.__context__ = error_snapshot[2]
            error.__suppress_context__ = error_snapshot[3]
            raise
    finally:
        caller_exception = None


async def bootstrap_storage_from_master_token(storage_path: Path) -> BootstrapOutcome:
    """Resolve the four-state cold-start bootstrap transaction."""
    caller_exception = sys.exc_info()[1]
    try:
        try:
            return await _bootstrapper(storage_path).bootstrap_storage(strict_loader=_strict_loader)
        except _BootstrapError as exc:
            error_context = exc.__context__
            if (
                exc.__cause__ is not None
                and exc.__suppress_context__
                and error_context is caller_exception
            ):
                error_context = exc.__cause__
            error_snapshot = (
                str(exc),
                exc.__cause__,
                error_context,
                exc.__suppress_context__,
            )
        caller_exception = None
        error = MasterTokenError(error_snapshot[0])
        try:
            raise error
        except MasterTokenError:
            error.__cause__ = error_snapshot[1]
            error.__context__ = error_snapshot[2]
            error.__suppress_context__ = error_snapshot[3]
            raise
    finally:
        caller_exception = None


async def bootstrap_missing_storage_from_master_token(storage_path: Path) -> bool:
    """Collapse cold-start outcomes to the historical CLI boolean."""
    outcome = await bootstrap_storage_from_master_token(storage_path)
    return outcome in (BootstrapOutcome.MINTED, BootstrapOutcome.PRESENT_AFTER_WAIT)


def _session_owner_reader(storage_path: Path) -> str | None:
    from .storage import get_account_email_for_storage  # noqa: PLC0415

    return get_account_email_for_storage(storage_path)


def _android_id_generator() -> str:
    return generate_android_id()


def _strict_loader(storage_path: Path) -> httpx.Cookies:
    from .cookies import _build_httpx_cookies_from_storage_strict  # noqa: PLC0415

    return _build_httpx_cookies_from_storage_strict(storage_path)


async def _verifier(storage_path: Path) -> int:
    return await _verify_by_listing_notebooks(storage_path)


def _bootstrapper(storage_path: Path) -> MasterTokenBootstrapper:
    return MasterTokenBootstrapper(
        mint_service=MintService(),
        store=ProfileStore(storage_path),
        bootstrap_lock=FileLock(str(_bootstrap_lock_path(storage_path))),
        verifier=_verifier,
    )
