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

import asyncio
import enum
import json
import logging
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock, Timeout

# perform_oauth for the OAuthLogin token rides the Chromecast app + signature
# (the spike confirmed the labs-tailwind app's sig downscopes; chromecast yields
# a uberauth-capable token; the labs-tailwind app's sig downscopes to email).
_MASTER_APP = "com.google.android.apps.chromecast.app"
_MASTER_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"
_OAUTHLOGIN_SERVICE = "oauth2:https://www.google.com/accounts/OAuthLogin"

# Aligned with ``_has_rotatable_secondary_binding``, NOT the strict
# ``_has_valid_secondary_binding``: this set feeds ``_recover_psidts_inline``, so
# it answers "is a rotation worth attempting", which is the permissive question.
# It is therefore *not* stale relative to the ``LSID`` conjunct added in #1977 —
# do not "fix" it by adding LSID here.
# MergeSession requires SID + a secondary binding (APISID+SAPISID or OSID) so the
# client's _recover_psidts_inline can mint __Secure-1PSIDTS on first load.
_REQUIRED_MINTED_COOKIES = {"SID", "APISID", "SAPISID"}

_MASTER_TOKEN_VERSION = 1

logger = logging.getLogger("notebooklm.auth.master_token")

# Serializes the global-logger save/restore in _quiet_gpsoauth_logging so
# overlapping re-mints on different threads (asyncio.to_thread) can't stomp each
# other's saved levels. ponytail: one process-wide lock; the window is one short
# sync RPC, so contention is negligible.
_LOG_LOCK = threading.Lock()


class MasterTokenError(Exception):
    """The master token (or its exchange) was rejected — re-bootstrap needed.

    Raised for revoked/expired master tokens, gpsoauth failures, and a minted
    cookie jar missing the cookies the web client needs. Carries no secrets.
    """


def _require_gpsoauth() -> Any:
    try:
        import gpsoauth  # noqa: PLC0415  (lazy: optional [headless] extra)
    except ImportError as exc:  # pragma: no cover - import guard
        raise MasterTokenError(
            "Master-token auth needs gpsoauth. Install: pip install 'notebooklm-py[headless]'"
        ) from exc
    return gpsoauth


@contextmanager
def _quiet_gpsoauth_logging() -> Iterator[None]:
    """Silence urllib3/requests DEBUG bodies around the gpsoauth call so the
    master token / ya29 in request bodies never reach a debug log sink."""
    names = ("urllib3", "requests", "urllib3.connectionpool")
    with _LOG_LOCK:
        saved = {n: logging.getLogger(n).level for n in names}
        try:
            for n in names:
                logging.getLogger(n).setLevel(logging.WARNING)
            yield
        finally:
            for n, lvl in saved.items():
                logging.getLogger(n).setLevel(lvl)


def generate_android_id() -> str:
    """Random stable 64-bit hex Android id, generated once per install and
    persisted with the token. Changing it can re-trip Google's new-device risk
    signal on re-mint, so callers must reuse the stored value."""
    return secrets.token_hex(8)


def exchange_master_token(email: str, oauth_token: str, android_id: str) -> str:
    """One-time: a single-use EmbeddedSetup ``oauth_token`` -> durable ``aas_et/``
    master token. Raises :class:`MasterTokenError` on rejection (no secret leak)."""
    gpsoauth = _require_gpsoauth()
    try:
        with _quiet_gpsoauth_logging():
            res = gpsoauth.exchange_token(email, oauth_token, android_id)
    except Exception as exc:  # noqa: BLE001 — any gpsoauth/transport failure; never leak the body
        raise MasterTokenError("exchange_token failed (network or gpsoauth error).") from exc
    token = res.get("Token")
    if not token:
        # res may carry Error/ErrorDetail (no secrets); include only the code.
        raise MasterTokenError(
            f"exchange_token rejected the oauth_token (Error={res.get('Error', 'unknown')}). "
            "The oauth_token is single-use and short-lived — re-capture it."
        )
    return str(token)


async def mint_cookies(email: str, master_token: str, android_id: str) -> httpx.Cookies:
    """Mint a fresh NotebookLM web cookie jar from the master token.

    perform_oauth (sync, run inline — it is a single short request) -> ya29, then
    OAuthLogin?issueuberauth=1 -> uberauth -> MergeSession -> Set-Cookie jar.
    Raises :class:`MasterTokenError` if the token is revoked or the jar lacks the
    cookies the web client needs.
    """
    gpsoauth = _require_gpsoauth()

    def _perform() -> Any:
        with _quiet_gpsoauth_logging():
            return gpsoauth.perform_oauth(
                email,
                master_token,
                android_id,
                service=_OAUTHLOGIN_SERVICE,
                app=_MASTER_APP,
                client_sig=_MASTER_SIG,
            )

    try:
        # perform_oauth is a sync (requests) network call — off-thread it so it
        # never blocks the event loop of a live client during layer-4 recovery.
        oauth = await asyncio.to_thread(_perform)
    except Exception as exc:  # noqa: BLE001 — any gpsoauth/transport failure; never leak the body
        raise MasterTokenError("perform_oauth failed (network or gpsoauth error).") from exc
    bearer = oauth.get("Auth")
    if not bearer:
        raise MasterTokenError(
            f"perform_oauth rejected the master token (Error={oauth.get('Error', 'unknown')}). "
            "Re-bootstrap with `notebooklm login --master-token`."
        )

    # Wrap the cookie-mint HTTP legs: an unwrapped httpx error would escape the
    # refresh path AND its ``.request.url`` embeds the uberauth token. Re-raise as
    # a secret-free MasterTokenError so the caller declines gracefully.
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            auth = {"Authorization": f"Bearer {bearer}"}
            uber = await client.get(
                "https://accounts.google.com/OAuthLogin",
                params={"source": "ChromiumBrowser", "issueuberauth": "1"},
                headers=auth,
            )
            uberauth = uber.text.strip()
            if uber.status_code != 200 or not uberauth or " " in uberauth:
                raise MasterTokenError("OAuthLogin did not return a uberauth token.")
            await client.get(
                "https://accounts.google.com/MergeSession",
                params={
                    "service": "mail",
                    "continue": "https://www.google.com",
                    "uberauth": uberauth,
                },
                headers=auth,
            )
            # Mint __Secure-1PSIDTS now too (the rotating freshness partner of
            # __Secure-1PSID) so the stored jar is complete and valid at rest — no
            # first-call recovery needed and `auth check` passes immediately.
            # ``_rotate_post`` is the same single-wire-contract RotateCookies
            # POST every other rotation path uses; it needs the SID +
            # APISID/SAPISID binding the MergeSession jar already carries.
            # Best-effort: Google may withhold it, and inline recovery remains
            # the fallback, so a failure here must not fail the mint.
            from .keepalive import _rotate_post  # noqa: PLC0415 (avoid import cycle)

            try:
                await _rotate_post(client)
            except httpx.HTTPError as exc:
                logger.debug("RotateCookies during mint failed (non-fatal): %s", exc)
            jar = httpx.Cookies()
            for cookie in client.cookies.jar:
                jar.jar.set_cookie(cookie)
    except httpx.HTTPError:
        raise MasterTokenError(
            "cookie minting failed (network error reaching accounts.google.com)."
        ) from None  # drop the httpx __cause__ whose URL carries the uberauth

    names = {c.name for c in jar.jar}
    missing = _REQUIRED_MINTED_COOKIES - names
    if missing:
        raise MasterTokenError(
            f"Minted cookie jar is missing required cookies: {sorted(missing)}. "
            "MergeSession may have changed; the session would fail PSIDTS recovery."
        )
    return jar


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
        # Mirrors _auth/account.write_account_metadata's namespace shape.
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
    :func:`notebooklm._auth.storage_writer.persist_minted_jar`, which routes the
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
    :func:`notebooklm._auth.storage_writer.persist_minted_jar` for why
    ``remint_from_stored_token`` passes ``False`` here."""
    from . import storage_writer  # noqa: PLC0415 (avoid import cycle)

    storage_writer.persist_minted_jar(
        path, jar, email=email, force=force, refuse_unknown_owner=refuse_unknown_owner
    )


# --- master_token.json persistence (mode 0600, beside storage_state.json) ---


def read_master_token(path: Path) -> dict[str, Any] | None:
    """Read a ``master_token.json`` record, or ``None`` if absent. Raises
    :class:`MasterTokenError` on a malformed/old-version file."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MasterTokenError(f"Unreadable master_token.json: {exc}") from exc
    if not isinstance(data, dict):  # e.g. a bare JSON array — avoid .get AttributeError
        raise MasterTokenError("master_token.json is malformed or an unsupported version.")
    required = ("master_token", "email", "android_id")
    if data.get("version") != _MASTER_TOKEN_VERSION or any(not data.get(k) for k in required):
        raise MasterTokenError("master_token.json is malformed or an unsupported version.")
    return data


def write_master_token(path: Path, *, email: str, master_token: str, android_id: str) -> None:
    """Persist a master-token record at mode 0600 (full-account credential).

    Delegates to :func:`notebooklm._auth.storage_writer.write_master_token`,
    which routes the write through ``_atomic_io`` (atomic + fsync-durable + temp
    cleanup) under a bounded sibling lock — closing the lockless-write half of
    [storage-F5]. This function stays as the ``notebooklm.auth``-exported facade
    symbol."""
    from . import storage_writer  # noqa: PLC0415 (avoid import cycle)

    storage_writer.write_master_token(
        path, email=email, master_token=master_token, android_id=android_id
    )


# --- the transaction (relocated from cli/services/login/master_token.py,
# #2103 structural follow-up PR-2): the CLI now invokes whole audited
# transactions below, never assembles minting primitives itself. ---


async def _verify_by_listing_notebooks(storage_path: Path) -> int:
    """Smoke-test a minted session: list notebooks. Returns the count."""
    from ..client import NotebookLMClient  # noqa: PLC0415 (avoid import cycle)

    async with NotebookLMClient.from_storage(path=str(storage_path)) as client:
        return len(await client.notebooks.list())


def assert_account_writable(*, email: str, storage_path: Path, force: bool = False) -> None:
    """Refuse to overwrite a profile that already belongs to a *different* Google
    account, unless ``force``. ``--account`` selects the account to mint; the
    profile selects where it lands — minting account B into account A's profile
    silently clobbers A's cookies *and* durable master token. Checks BOTH the
    stored session and the existing ``master_token.json`` owner, since either can
    be present without the other (e.g. a stale storage file beside a token for a
    different account).

    A one-path signature (#2103 PR-2 D2): ``master_token_path`` is derived
    internally via :func:`notebooklm.paths.master_token_path_for` rather than
    taken as a parameter, so every caller above this chokepoint passes only
    ``storage_path``.

    This is a fail-fast ADVISORY check only — called before the (possibly
    ~300s) interactive ``oauth_token`` capture so a wrong profile fails fast
    instead of after a full sign-in. It cannot itself close the TOCTOU window
    between this check and the mint completing (the mint hasn't started yet).
    The AUTHORITATIVE, race-free enforcement lives under the storage-write
    lock in :func:`notebooklm._auth.storage_writer.persist_minted_jar`
    (#2103 PR-2 D6) — this function is a courtesy, not the guard."""
    if force:
        return
    if not email:
        # Type says str, but this is a public library boundary (notebooklm.
        # auth.assert_account_writable) — fail with a typed error rather than
        # a raw AttributeError from `email.casefold()` below (#2103 PR-2 review).
        raise MasterTokenError("assert_account_writable requires a non-empty email.")
    from ..paths import master_token_path_for  # noqa: PLC0415 (avoid import cycle)
    from .account import get_account_email_for_storage  # noqa: PLC0415 (avoid import cycle)

    master_token_path = master_token_path_for(storage_path)
    try:
        token_rec = read_master_token(master_token_path)
    except MasterTokenError:
        token_rec = None  # malformed token will be re-bootstrapped; not an owner signal
    owners = {
        owner.strip()
        for owner in (get_account_email_for_storage(storage_path), (token_rec or {}).get("email"))
        if isinstance(owner, str) and owner.strip()
    }
    conflict = next((o for o in owners if o.casefold() != email.casefold()), None)
    if conflict:
        raise MasterTokenError(
            f"This profile already belongs to {conflict}, but --account is {email}. "
            f"Minting here would overwrite {conflict}'s session and master token. "
            "Use a dedicated profile (e.g. `notebooklm -p <name> login --master-token "
            f"--account {email}`), or pass --force to overwrite this one."
        )


def _resolve_bootstrap_android_id(master_token_path: Path, *, explicit: str | None) -> str:
    """Explicit ``android_id`` wins; else reuse the id stored in an existing
    ``master_token.json``; else generate a fresh one (#2103 PR-2 D5 — resolved
    inside the library so every caller gets identity continuity for free).
    Reusing the stored id (rather than resetting it, even under ``--force``)
    matters because changing it can re-trip Google's new-device risk signal on
    re-mint.

    Does NOT swallow :class:`MasterTokenError` (#2103 PR-2 review):
    ``read_master_token`` returns ``None`` only when the file is ABSENT, and
    raises for a present-but-corrupted one. Before this function existed,
    the pre-PR CLI driver's unwrapped
    ``read_master_token(master_token_path)`` call raised the same corruption
    loudly, before any capture or mint. Letting it propagate here (rather
    than silently generating a fresh id and overwriting a possibly-recoverable
    token) preserves that for every caller of :func:`bootstrap_from_oauth_token`
    — including a direct library caller of ``notebooklm.auth.master_token_bootstrap``,
    which the CLI's own separate pre-capture probe (``master_token_login.py``)
    does not cover."""
    if explicit:
        return explicit
    record = read_master_token(master_token_path)
    return record["android_id"] if record is not None else generate_android_id()


async def bootstrap_from_oauth_token(
    *,
    email: str,
    oauth_token: str,
    storage_path: Path,
    android_id: str | None = None,
    verify: bool = True,
    force: bool = False,
) -> int:
    """One-time: exchange the single-use ``oauth_token`` for a durable master
    token, persist it (0600), mint cookies, write ``storage_state.json``, and
    (optionally) verify by listing notebooks. Returns the notebook count (or -1
    when verify is False). Raises :class:`MasterTokenError` on rejection.

    Refuses to overwrite a profile that already belongs to a *different*
    account (``--account`` mismatch) unless ``force`` — minting writes a full
    session + durable token into the profile, so a wrong profile silently
    clobbers it. See :func:`assert_account_writable` for the fail-fast
    pre-check and :func:`notebooklm._auth.storage_writer.persist_minted_jar`
    for the authoritative, lock-guarded enforcement.

    ``android_id`` defaults to ``None``, resolved explicit -> stored -> fresh
    (#2103 PR-2 D5); pass it explicitly only to override.

    Writes ``master_token.json`` (the durable, infostealer-grade credential)
    only AFTER ``persist_minted_jar``'s authoritative ownership check has
    already succeeded (#2103 PR-2 review): the original ordering wrote the
    durable token first and gated only
    ``storage_state.json``, so a profile with no recorded owner on EITHER
    file (``assert_account_writable``'s advisory pre-check sees no conflict
    when nothing is recorded yet) would durably persist a master token for
    the mint's account, THEN fail at the storage gate — leaving a live
    credential on disk for an operation the caller was told was refused.
    ``persist_minted_jar`` needs nothing from ``write_master_token`` (it
    takes the minted jar directly), so reordering costs nothing."""
    from ..paths import master_token_path_for  # noqa: PLC0415 (avoid import cycle)

    master_token_path = master_token_path_for(storage_path)
    aid = await asyncio.to_thread(
        _resolve_bootstrap_android_id, master_token_path, explicit=android_id
    )
    await asyncio.to_thread(
        assert_account_writable, email=email, storage_path=storage_path, force=force
    )
    # exchange/mint/persist are sync (network + locked file I/O) — off-thread so
    # they don't block the event loop the CLI runs them on.
    token = await asyncio.to_thread(exchange_master_token, email, oauth_token, aid)
    jar = await mint_cookies(email, token, aid)
    await asyncio.to_thread(persist_minted_jar, storage_path, jar, email=email, force=force)
    # Only reached once the authoritative gate above has actually passed.
    await asyncio.to_thread(
        write_master_token, master_token_path, email=email, master_token=token, android_id=aid
    )
    return await _verify_by_listing_notebooks(storage_path) if verify else -1


async def remint_from_stored_token(storage_path: Path) -> httpx.Cookies:
    """No-prompt re-mint from the stored master token (recovery / hand-run).

    The one read -> mint -> persist -> reload sequence every re-mint caller
    needs (#2103 PR-2 D1) — before this PR, the L4 recovery rung
    (``_auth/recovery.py``) and the CLI's operator-refresh path each assembled
    it independently and disagreed on error handling and reload. Overwrites
    ``storage_state.json`` with a fresh session. Raises :class:`MasterTokenError`
    if no master token is on record, or if the exchange/mint is rejected.

    Reloads via the strict, side-effect-free loader
    (:func:`notebooklm._auth.cookies._build_httpx_cookies_from_storage_strict`)
    rather than :func:`notebooklm._auth.cookies.build_httpx_cookies_from_storage`
    (which would trigger a NETWORK POST + storage write via inline PSIDTS
    recovery right after this function's own write — redundant at best, a
    second unwanted mutation at worst). Callers that need the existing
    recovery-loader reload semantics (the L4 rung) perform their own reload
    afterward instead of trusting this return value for that purpose.

    Persists with ``refuse_unknown_owner=False`` (#2103 PR-2 D6): this
    function re-mints from a master token ALREADY paired with ``storage_path``
    (structurally, via :func:`notebooklm.paths.master_token_path_for` —
    they're sibling files in the same profile), so no account is being
    *selected* here the way ``bootstrap_from_oauth_token`` selects one via
    ``--account``. Requiring pre-existing in-band account metadata would
    break mid-session self-recovery for the common case of a profile that
    was never bound to an explicit account (e.g. a cookie-only
    ``import-cookies`` profile) — the "different recorded owner" refusal
    still applies unconditionally."""
    from ..paths import master_token_path_for  # noqa: PLC0415 (avoid import cycle)

    master_token_path = master_token_path_for(storage_path)
    record = await asyncio.to_thread(read_master_token, master_token_path)
    if record is None:
        raise MasterTokenError(
            f"No master token at {master_token_path}. Run `notebooklm login --master-token` first."
        )
    jar = await mint_cookies(record["email"], record["master_token"], record["android_id"])
    await asyncio.to_thread(
        persist_minted_jar,
        storage_path,
        jar,
        email=record.get("email"),
        refuse_unknown_owner=False,
    )

    from .cookies import _build_httpx_cookies_from_storage_strict  # noqa: PLC0415

    try:
        return await asyncio.to_thread(_build_httpx_cookies_from_storage_strict, storage_path)
    except (OSError, ValueError) as exc:
        # The mint+persist above already succeeded — reaching here means the
        # freshly-written file itself failed to reload (e.g. the best-effort
        # RotateCookies leg of ``mint_cookies`` was withheld by Google, so the
        # persisted jar is missing the Tier-1 ``__Secure-1PSIDTS`` cookie the
        # strict loader requires; ``RequiredCookieValidationError`` is a
        # ``ValueError``). Surface this uniformly as MasterTokenError — the
        # one exception type every caller of this "raising _auth primitive"
        # (#2103 PR-2 D1) already handles — rather than letting a raw
        # loader exception escape a function documented to raise only this.
        raise MasterTokenError(
            f"Master token re-mint persisted a session but reloading it failed "
            f"({type(exc).__name__}): {exc}"
        ) from exc


class BootstrapOutcome(enum.Enum):
    """Four-state result of :func:`bootstrap_storage_from_master_token`
    (#2103 PR-2 D7) — replaces a plain boolean that conflated "I minted it"
    with "someone else already had", and "nothing to do because storage
    already existed" with "nothing to do because there is no token"."""

    MINTED = "minted"
    """This call performed the mint and persisted fresh storage."""
    PRESENT_AFTER_WAIT = "present_after_wait"
    """Storage appeared while this call waited for the bootstrap lock — a
    concurrent leader minted it first."""
    PRESENT_ON_ENTRY = "present_on_entry"
    """Storage already existed before this call did anything; no bootstrap
    was attempted."""
    NO_TOKEN = "no_token"
    """No sibling master token exists, so there is nothing to bootstrap from."""


def _bootstrap_lock_path(storage_path: Path) -> Path:
    """Return the canonical lock that serializes first-time session minting.

    Degrades to a best-effort (non-canonicalized) path on a circular symlink
    rather than raising, matching :func:`notebooklm.paths.master_token_path_for`
    (#2103 PR-1): ``Path.resolve()`` raises ``RuntimeError`` (not ``OSError``)
    on a symlink loop on Python 3.10-3.12 (fixed upstream in 3.13). Found by
    CodeRabbit during the combined PR review — this call site does its own
    separate ``expanduser().resolve()`` rather than going through the shared
    chokepoint (it derives a *lock* path, not the master-token sibling), so it
    hadn't inherited PR-1's fix."""
    expanded = storage_path.expanduser()
    try:
        canonical_path = expanded.resolve()
    except (OSError, RuntimeError):
        canonical_path = expanded
    return canonical_path.with_name(f".{canonical_path.name}.lock.bootstrap")


async def _acquire_bootstrap_lock(lock: FileLock) -> None:
    """Acquire without blocking the event loop, including on filelock 3.13."""
    while True:
        try:
            lock.acquire(blocking=False)
        except Timeout:
            await asyncio.sleep(0.05)
        else:
            return


async def _run_remint_to_settlement(storage_path: Path) -> None:
    """Do not release the bootstrap lock while the re-mint's persist is still
    running (its final write is offloaded to a thread)."""
    task = asyncio.create_task(remint_from_stored_token(storage_path))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Propagating caller cancellation immediately would release the
        # bootstrap lock while the offloaded write can still be running,
        # allowing a waiting process to mint again concurrently.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - settle before cancellation propagates
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def bootstrap_storage_from_master_token(storage_path: Path) -> BootstrapOutcome:
    """Mint initial storage when only the sibling master token exists
    (#2103 PR-2 D7 — the bootstrap flock/shield/recheck machinery, relocated
    from ``cli/services/auth_refresh.py`` so the CLI driver reduces to a thin
    call).

    Not an ADR-0030 recovery rung: this is a cold-start ENTRY POINT (called
    once, before any client exists), with its own dedicated bootstrap lock —
    distinct from the storage-write lock ``remint_from_stored_token`` acquires
    while persisting, since holding that lock here would self-deadlock.

    Logs the resolved outcome at DEBUG (#2103 PR-2 review): the whole point
    of a 4-state result instead of a bool is to make "I minted it"
    distinguishable from "a concurrent leader already had" — that
    distinction is otherwise invisible in logs, same as before this type
    existed."""
    outcome = await _resolve_bootstrap_outcome(storage_path)
    logger.debug("bootstrap_storage_from_master_token(%s) -> %s", storage_path, outcome)
    return outcome


async def bootstrap_missing_storage_from_master_token(storage_path: Path) -> bool:
    """Mint initial storage when only the sibling master token exists,
    collapsed to the boolean ``cli/services/auth_refresh.py`` has always
    used (auth cross-boundary ledger shrink, follow-up to #2103 PR-2/PR-3):
    ``MINTED`` and ``PRESENT_AFTER_WAIT`` both mean "storage is ready, take
    the mandatory passive-validation path"; ``PRESENT_ON_ENTRY`` and
    ``NO_TOKEN`` both mean "nothing was bootstrapped here, enter ordinary
    recovery".

    ``BootstrapOutcome`` itself stays internal to this module — its only
    real first-party importer collapsed it to a bool immediately after the
    call, so publishing the enum across the CLI boundary bought no caller
    anything (the DEBUG log two lines up already gives the fine-grained
    observability the type was introduced for). Callers that need the raw
    4-state result should call :func:`bootstrap_storage_from_master_token`
    directly (available inside ``_auth``, not across the boundary)."""
    outcome = await bootstrap_storage_from_master_token(storage_path)
    return outcome in (BootstrapOutcome.MINTED, BootstrapOutcome.PRESENT_AFTER_WAIT)


async def _resolve_bootstrap_outcome(storage_path: Path) -> BootstrapOutcome:
    # Preserve the healthy-storage fast path: profiles that already have a jar
    # should not pay for a lock merely because they also retain a master token.
    if storage_path.exists():
        return BootstrapOutcome.PRESENT_ON_ENTRY
    from ..paths import master_token_path_for  # noqa: PLC0415 (avoid import cycle)

    master_token_path = master_token_path_for(storage_path)
    if not master_token_path.exists():
        return BootstrapOutcome.NO_TOKEN

    lock = FileLock(str(_bootstrap_lock_path(storage_path)))
    await _acquire_bootstrap_lock(lock)
    try:
        # A leader may have created storage while this caller waited. Recheck
        # both inputs after waiting: another process may have completed the
        # bootstrap, or removed the durable token, in the meantime.
        if storage_path.exists():
            return BootstrapOutcome.PRESENT_AFTER_WAIT
        if not master_token_path.exists():
            return BootstrapOutcome.NO_TOKEN
        await _run_remint_to_settlement(storage_path)
        return BootstrapOutcome.MINTED
    finally:
        # Release synchronously so cancellation cannot strand the process-wide
        # lock between a completed mint and an await scheduled for cleanup.
        lock.release()
