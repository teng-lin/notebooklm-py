"""Self-hosted OAuth 2.1 authorization server for the remote (HTTP) MCP transport.

claude.ai's custom-connector UI speaks only OAuth (no bearer field), so to reach the
server from claude.ai we must BE an OAuth authorization server. Rather than depend on
an external IdP, this runs a tiny single-tenant AS gated by one **password**, composed
with the Phase A bearer via :class:`~fastmcp.server.auth.MultiAuth` (so Claude Code /
Desktop keep using the bearer; claude.ai uses OAuth — one server, both clients).

Design (converged review — codex/claude/agy):

* Subclass :class:`InMemoryOAuthProvider`, which already implements the full OAuth 2.1
  flow (DCR, PKCE, token issue/refresh/revoke, metadata) CORRECTLY. Its only "testing"
  traits are in-memory storage and an auto-approve ``authorize()`` — both addressed
  here. We do NOT hand-roll the OAuth protocol.
* **Password gate without touching ``/authorize``**: the MCP-SDK ``AuthorizationHandler``
  validates client/redirect_uri/scope/PKCE BEFORE calling ``provider.authorize()`` and
  302s the browser to whatever it returns. So we override ``authorize()`` to stash the
  ALREADY-VALIDATED ``(client, params)`` under a single-use ``sid`` and return a
  ``/login?sid=`` URL. The validated ``redirect_uri`` never enters the browser → open
  redirect is structurally impossible; the ``sid`` doubles as the CSRF token.
* ``/login`` (a public route added in ``get_routes``) renders a password form (only the
  ``sid`` + a password field — nothing attacker-controlled, so no XSS) and, on a
  constant-time password match, calls the PARENT ``authorize`` to issue the code.

Hardening: strong-password startup check (primary brute-force defense), per-IP login
throttle, capped DCR + capped pending-stash (pre-auth DoS), and atomic file persistence
of clients+tokens (reusing ``_atomic_io``) so a redeploy doesn't force re-auth.

Residuals (low-severity, single-user; reviewed by a security panel — all Low/Info, no
account-compromise without phishing a human or pre-owning the disk, which already
exposes the co-located full-account ``master_token.json``):

* login-CSRF/phishing — an attacker who registers their own client (open DCR) and
  phishes the owner into entering the password on the attacker's ``/login`` link could
  authorize that client. The ``/login`` page shows the (escaped) redirect target so a
  rogue client is noticeable before the password is typed, and the owner only logs in
  from claude.ai's own flow — phishing-class, bounded by the strong password.
* ``/authorize`` flood vs the owner's login — eviction (oldest-first) keeps the
  owner's authorize from being *rejected*, but a sustained pre-auth flood can still
  evict the owner's idle in-flight ``sid`` BEFORE they submit the password ("login link
  expired"); the owner simply retries. Availability-only; bounded by the 300s TTL.
* persisted tokens — refresh tokens are long-lived and written (0600) to the OAuth
  state file (path logged at startup; default ``<home>/oauth/<slug>.json`` keyed on the
  base_url — see :func:`get_oauth_config`); treat it as a FULL-ACCOUNT secret (same tier
  as ``master_token.json``). Real revocation = delete THAT file + restart; a legacy
  profile-dir ``oauth_state.json`` migrated once is renamed ``.migrated`` and never
  re-read, so deleting the live file does not resurrect its tokens. Rotating
  ``NOTEBOOKLM_MCP_OAUTH_PASSWORD`` does NOT revoke already-issued tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import anyio
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from fastmcp.utilities.ui import create_secure_html_response
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from notebooklm._atomic_io import atomic_update_json, atomic_write_json
from notebooklm.paths import get_home_dir, get_profile_dir

from ._urlcheck import _validate_bare_https_origin

logger = logging.getLogger(__name__)

__all__ = [
    "OAUTH_BASE_URL_ENV",
    "OAUTH_PASSWORD_ENV",
    "OAUTH_STATE_PATH_ENV",
    "TRUST_PROXY_ENV",
    "OAuthConfig",
    "SelfHostedOAuthProvider",
    "build_oauth_provider",
    "get_oauth_config",
]

#: Env vars (env-only; never CLI flags → no `ps aux` leak). Both required together to
#: enable OAuth; unset → bearer-only (Phase A unchanged).
OAUTH_PASSWORD_ENV = "NOTEBOOKLM_MCP_OAUTH_PASSWORD"
OAUTH_BASE_URL_ENV = "NOTEBOOKLM_MCP_OAUTH_BASE_URL"
#: Optional override for where the OAuth client registry + issued tokens persist. When
#: set (non-empty) it wins over the derived default; used verbatim as a path
#: (operator-supplied, so no traversal guard). Unset → ``<home>/oauth/<slug>.json`` keyed
#: on the base_url (the AS issuer) — deployment-scoped, NOT tied to the served account
#: profile, so switching profiles no longer orphans registered clients.
OAUTH_STATE_PATH_ENV = "NOTEBOOKLM_MCP_OAUTH_STATE_PATH"
#: Opt-in: trust the proxy-set ``CF-Connecting-IP`` header as the login-throttle key
#: (``"1"`` to enable). Default off → key on the socket peer. Only set this when a trusted
#: proxy (e.g. the Cloudflare tunnel) authoritatively sets the header; if the origin is
#: exposed directly, a client could forge ``CF-Connecting-IP`` to dodge the per-IP throttle.
TRUST_PROXY_ENV = "NOTEBOOKLM_MCP_TRUST_PROXY"

#: The password is the gate, so insist it's strong (the primary brute-force defense).
MIN_PASSWORD_LEN = 16
#: Bounds on pre-auth, unauthenticated state (DoS hardening). DCR + the pending-login
#: stash are both reachable WITHOUT the password.
MAX_CLIENTS = 100
MAX_PENDING = 20
PENDING_TTL_SECONDS = 300
MAX_LOGIN_ATTEMPTS = 3
#: Per-IP login throttle: at most THROTTLE_MAX failed POSTs per THROTTLE_WINDOW seconds.
THROTTLE_WINDOW_SECONDS = 60
THROTTLE_MAX_FAILURES = 5
#: Cap the number of distinct IPs tracked for throttling (bound pre-auth memory).
MAX_THROTTLE_IPS = 2048


class _Pending(NamedTuple):
    """A pre-password, SDK-validated authorize request awaiting the password page."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    expiry: float
    attempts: int


@dataclass(frozen=True)
class OAuthConfig:
    """Resolved + validated self-hosted-OAuth config."""

    # repr=False so the cleartext password never lands in an exception/debug dump of
    # the config object (the provider keeps only a digest; this object must hold the
    # cleartext to construct it, so keep it out of repr).
    password: str = field(repr=False)
    base_url: str = field(repr=True)
    state_path: Path | None = field(default=None)  # persist target; None → no persistence
    # One-time migration source: the pre-#1765-reversal profile-dir oauth_state.json.
    # Read ONCE at startup if ``state_path`` does not exist yet, then renamed ``.migrated``.
    legacy_state_path: Path | None = field(default=None)
    # Trust the proxy-set CF-Connecting-IP header for throttle keying (default off — the
    # socket peer is used unless an operator asserts a trusted proxy sets the header).
    trust_proxy: bool = field(default=False)


def _slug_for_base_url(base_url: str) -> str:
    """Filesystem-safe, collision-resistant filename stem for an OAuth issuer.

    A readable ``netloc`` prefix (cosmetic) plus a 64-bit hash of the normalized origin
    (the disambiguator). Distinct origins → distinct hash → distinct file, even when the
    sanitized prefix collides — :func:`_validate_bare_https_origin` restricts neither the
    host charset nor the port, so the prefix alone is not unique.
    """
    norm = base_url.strip().rstrip("/").lower()
    # Cap the readable prefix so a pathologically long hostname can't push the filename
    # past the 255-byte limit; the hash (over the FULL origin) still disambiguates.
    readable = (re.sub(r"[^a-z0-9._-]", "_", urlsplit(base_url).netloc.lower()) or "origin")[:64]
    digest = hashlib.sha256(norm.encode()).hexdigest()[:16]
    return f"{readable}.{digest}"


def _derived_state_path(base_url: str) -> Path:
    """Default OAuth-state path ``<home>/oauth/<slug(base_url)>.json``.

    Keyed on the base_url (the AS issuer), so it is deployment-scoped: two servers with
    different issuers get different files; one server keeps its file across a
    served-account switch (the base_url is unchanged)."""
    return get_home_dir() / "oauth" / f"{_slug_for_base_url(base_url)}.json"


def _migrate_legacy_state(state_path: Path, legacy_path: Path | None) -> None:
    """One-time, non-destructive migration of a legacy profile-dir ``oauth_state.json``.

    Best-effort and never fatal: any failure is logged and startup continues. The
    ``.migrated`` rename is the durable "already handled" marker — it is independent of
    ``state_path`` (so it survives a revocation that deletes the live file), and it is
    committed by renaming the legacy file BEFORE the migrated state is exposed at
    ``state_path``. That ordering is what makes revocation safe even if the write fails:
    a failed write rolls the rename back for a clean retry, and there is never a window
    where a live ``state_path`` coexists with a still-migratable legacy file. Concurrent
    startups are safe — only one wins the atomic rename; the loser sees the legacy gone.
    """
    if legacy_path is None or not legacy_path.exists():
        return
    # If an explicit override points the live state AT the legacy file itself, there is
    # nothing to migrate — and retiring it would delete the very file the provider loads.
    if legacy_path == state_path or (state_path.exists() and legacy_path.samefile(state_path)):
        return
    migrated = legacy_path.with_name(legacy_path.name + ".migrated")
    # Migration already happened for this deployment — either the ``.migrated`` marker
    # exists (a reappeared oauth_state.json is a backup restore or leftover, and importing
    # it could resurrect revoked tokens) or the live state file is present (a prior rename
    # failed/crashed). Retire the reappeared/lingering legacy so it is never re-migrated,
    # and never overwrite the authoritative live state.
    if migrated.exists() or state_path.exists():
        with contextlib.suppress(OSError):
            os.replace(legacy_path, migrated)
        return
    try:
        raw = legacy_path.read_bytes()
    except OSError as exc:  # transient read error — leave in place, retry next startup
        logger.warning("Could not read legacy OAuth state %s (will retry): %s", legacy_path, exc)
        return
    try:
        data = json.loads(raw)  # bytes: invalid UTF-8 or bad JSON → ValueError family
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # Corrupt or non-object payload: retire it (so it isn't re-parsed every startup)
        # WITHOUT writing it as state.
        logger.warning("Legacy OAuth state %s is unusable; retiring without migrating", legacy_path)
        with contextlib.suppress(OSError):
            os.replace(legacy_path, migrated)
        return
    try:
        if os.name == "posix":
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            state_path.parent.mkdir(parents=True, exist_ok=True)
        # Commit the marker FIRST (os.replace is atomic + cross-platform, unlike
        # Path.rename which raises on Windows if the dest exists): retire the legacy
        # source before exposing migrated state, so no failure past this point can leave
        # a resurrectable legacy file beside a live state_path.
        os.replace(legacy_path, migrated)
    except OSError as exc:
        logger.warning(
            "Skipped legacy OAuth-state migration (retiring %s failed): %s", legacy_path, exc
        )
        return
    try:
        # Locked (atomic_update_json wraps a filelock) + race-safe: keep a concurrent
        # startup's fresh state rather than clobbering it with the legacy copy.
        atomic_update_json(state_path, lambda cur: cur if cur else data, mode=0o600)
    except (OSError, ValueError) as exc:
        # Write failed after the marker committed — roll the rename back so the next
        # startup retries cleanly instead of stranding the data in ``.migrated``.
        with contextlib.suppress(OSError):
            os.replace(migrated, legacy_path)
        logger.warning("Legacy OAuth-state migration write failed (rolled back): %s", exc)
        return
    logger.info("Migrated legacy OAuth state %s → %s", legacy_path, state_path)


def get_oauth_config(profile: str | None = None) -> OAuthConfig | None:
    """Resolve the OAuth config from env, or ``None`` when OAuth is off.

    OFF when neither var is set. When EITHER is set the user intends OAuth, so BOTH
    are required (fail closed), the password must clear a strength bar, and the base
    URL must be https (the MCP SDK rejects non-HTTPS non-localhost issuers anyway).

    ``state_path`` (where issued tokens + registered clients persist, 0600) is
    **deployment-scoped**, keyed on the ``base_url`` (the OAuth issuer): the
    ``NOTEBOOKLM_MCP_OAUTH_STATE_PATH`` override wins, else ``<home>/oauth/<slug>.json``
    (:func:`_derived_state_path`). This intentionally REVERSES #1765's profile-dir
    coupling — the client registry is a property of the server, not the served account,
    so switching profiles no longer orphans registered clients. ``legacy_state_path``
    (the old profile-dir ``oauth_state.json``, resolved best-effort via ``profile`` —
    ``None`` if the profile is malformed) is migrated once at startup, see
    :func:`_migrate_legacy_state`.

    Args:
        profile: Auth profile whose legacy ``oauth_state.json`` is the one-time
            migration source. ``None`` resolves the active profile.

    Raises:
        SystemExit: partial/invalid OAuth config (missing var, weak password, bad URL).
    """
    password = os.environ.get(OAUTH_PASSWORD_ENV) or ""
    base_url = (os.environ.get(OAUTH_BASE_URL_ENV) or "").strip()
    if not password and not base_url:
        return None  # OAuth off — bearer-only (Phase A).

    if not password or not base_url:
        missing = OAUTH_PASSWORD_ENV if not password else OAUTH_BASE_URL_ENV
        raise SystemExit(
            f"Self-hosted OAuth is partially configured; set BOTH {OAUTH_PASSWORD_ENV} "
            f"and {OAUTH_BASE_URL_ENV} (or unset both to stay bearer-only). Missing: {missing}."
        )
    if len(password) < MIN_PASSWORD_LEN:
        raise SystemExit(
            f"{OAUTH_PASSWORD_ENV} is too weak: it gates a full-account credential and is "
            f"the primary brute-force defense, so it must be at least {MIN_PASSWORD_LEN} "
            "characters (use a long random value)."
        )
    # Must be a BARE https origin: the OAuth routes (/authorize, /token, /register,
    # /login, /.well-known/*) mount at the ROOT, so a path like /mcp would make the
    # discovery metadata advertise endpoints that don't exist. (A trailing "/" is
    # fine.) The same check guards the file-transfer base URL — shared helper.
    _validate_bare_https_origin(base_url, OAUTH_BASE_URL_ENV)

    # Deployment-scoped OAuth-state path, keyed on the base_url (the AS issuer), NOT the
    # served profile (#1765 reversal — see the docstring). An explicit override wins.
    override = os.environ.get(OAUTH_STATE_PATH_ENV)
    state_path: Path = Path(override).expanduser() if override else _derived_state_path(base_url)
    # One-time migration source: the old profile-dir oauth_state.json. Best-effort — a
    # malformed profile name (get_profile_dir raises ValueError) just means "no legacy to
    # migrate", NOT a startup failure (the profile guard still fires where the client
    # runtime actually uses it for storage).
    try:
        legacy_state_path: Path | None = get_profile_dir(profile) / "oauth_state.json"
    except ValueError:
        legacy_state_path = None

    # Opt-in trusted-proxy: only reached once OAuth is fully configured, so it never
    # fires on the OAuth-off path. Default off keeps the throttle keyed on the socket peer.
    trust_proxy = os.environ.get(TRUST_PROXY_ENV) == "1"

    return OAuthConfig(
        password=password,
        base_url=base_url,
        state_path=state_path,
        legacy_state_path=legacy_state_path,
        trust_proxy=trust_proxy,
    )


def _client_ip(request: Request, *, trust_proxy: bool) -> str:
    """Best-effort client IP for per-IP login throttling.

    The proxy-set ``CF-Connecting-IP`` header is trusted ONLY when ``trust_proxy`` is set
    (``NOTEBOOKLM_MCP_TRUST_PROXY=1``) — i.e. the operator asserts a trusted proxy (the
    Cloudflare tunnel) authoritatively sets it. Default off: an exposed-directly origin
    would otherwise let a client forge ``CF-Connecting-IP`` to dodge the per-IP throttle,
    so we key on the socket peer instead. With the flag off behind a tunnel the peer is the
    tunnel egress, degrading the throttle to a single global bucket — strictly MORE
    restrictive, never a spoof bypass. Secondary defense either way; the strong-password
    check is the real wall. A present-but-empty header falls back to the peer so it can't
    poison the bucket with a ``""`` key."""
    if trust_proxy:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf
    return request.client.host if request.client else "unknown"


class SelfHostedOAuthProvider(InMemoryOAuthProvider):
    """``InMemoryOAuthProvider`` + a password-gated ``/authorize``, with DoS bounds and
    file-backed persistence of clients + tokens."""

    def __init__(
        self,
        password: str,
        base_url: str,
        state_path: Path | None = None,
        trust_proxy: bool = False,
    ) -> None:
        super().__init__(
            base_url=base_url,
            # DCR is OFF by default — without this NO /register route is mounted and
            # claude.ai cannot register itself, so the whole OAuth path is dead.
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
        # Store only a non-reversible KDF digest of the gate password (never the
        # cleartext). scrypt (a deliberately slow, memory-hard KDF) rather than a bare
        # SHA-256 so the password — even though it's high-entropy and never persisted —
        # gets a computationally-expensive hash, the textbook treatment. A per-process
        # random salt is held in memory and used for BOTH the configured digest and each
        # presented-password digest, so equal passwords still produce equal digests for
        # the constant-time compare.
        self.__salt = secrets.token_bytes(16)
        self.__pw_digest = self._kdf(password)
        # Bound concurrent scrypt computations: each is ~tens of ms + ~16-64MB, and the
        # /login POST is unauthenticated, so cap how many run at once (≤4 ⇒ ≤256MB) and
        # run them OFF the event loop so a burst can't stall request servicing.
        self._kdf_limiter = anyio.CapacityLimiter(4)
        self._state_path = state_path
        self._trust_proxy = trust_proxy
        # Serialize saves. ``_save_state`` snapshots in-memory state on the loop
        # then offloads the atomic write to a worker thread; without this lock
        # two concurrent saves could snapshot in one order but have their fsyncs
        # complete in the opposite order, letting an OLDER snapshot's write
        # clobber a newer one (dropping a just-registered client/token, or
        # resurrecting a revoked token on the next restart). Holding the lock
        # across snapshot+offloaded-write makes a later save snapshot only after
        # the earlier write has landed, so the last save started always wins.
        self._save_lock = asyncio.Lock()
        # sid -> _Pending(client, validated params, expiry_ts, attempts). Pre-auth + bounded.
        self._pending: dict[str, _Pending] = {}
        # per-IP failed-login timestamps (throttle).
        self._fail_times: dict[str, list[float]] = {}
        self._load_state()

    def _kdf(self, password: str) -> bytes:
        """scrypt KDF of the password with the per-process salt (slow + memory-hard)."""
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=self.__salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )

    # -- DCR cap ---------------------------------------------------------------
    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Cap only NEW registrations so RFC 7591 updates to an existing client still work.
        if client_info.client_id not in self.clients and len(self.clients) >= MAX_CLIENTS:
            # DCR is open, so DON'T let a flood of throwaway registrations permanently
            # block the owner's onboarding: evict a TOKEN-LESS client (registered but
            # never completed a token exchange) to make room. Only if every client is
            # actively token-holding do we reject (real capacity, not an attack).
            used = {t.client_id for t in self.access_tokens.values()}
            used |= {t.client_id for t in self.refresh_tokens.values()}
            evictable = next((cid for cid in self.clients if cid not in used), None)
            if evictable is None:
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="Client registration limit reached.",
                )
            self.clients.pop(evictable, None)
        await super().register_client(client_info)
        await self._save_state()

    # -- password gate ---------------------------------------------------------
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Reached ONLY after the SDK handler validated client/redirect_uri/scope/PKCE.
        Stash the validated request and divert the browser to the password page."""
        self._prune_pending()
        # Bound the stash by EVICTING the oldest pending login rather than rejecting the
        # new one — DCR is open, so a flood of pre-password /authorize calls must NOT be
        # able to block the owner from starting a login (eviction keeps the cap without
        # the owner-lockout that raising would cause).
        while len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda s: self._pending[s].expiry)
            self._pending.pop(oldest, None)
        sid = secrets.token_urlsafe(32)
        self._pending[sid] = _Pending(client, params, time.time() + PENDING_TTL_SECONDS, 0)
        return f"{str(self.base_url).rstrip('/')}/login?sid={sid}"

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        # Public OAuth routes (authorize/token/register/.well-known) from the parent,
        # PLUS our public /login page. (Do NOT swap /authorize; do NOT call create_auth_routes.)
        routes = super().get_routes(mcp_path)
        routes.append(Route("/login", self._login, methods=["GET", "POST"]))
        return routes

    async def _login(self, request: Request) -> Response:
        if request.method == "GET":
            self._prune_pending()
            sid = request.query_params.get("sid", "")
            entry = self._pending.get(sid)
            # Show WHICH client/redirect the owner is authorizing (consent transparency)
            # so a rogue registered client is noticeable before the password is typed.
            redirect = str(entry.params.redirect_uri) if entry else None
            return self._render_form(sid, redirect_uri=redirect)

        ip = _client_ip(request, trust_proxy=self._trust_proxy)
        retry_after = self._throttled(ip)
        if retry_after is not None:
            return Response(
                "Too many attempts. Try again later.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        form = await request.form()
        sid = str(form.get("sid", ""))
        password = str(form.get("password", ""))

        self._prune_pending()
        entry = self._pending.get(sid)
        if entry is None:
            return self._render_form(
                "", error="Your login link expired. Restart the connection from claude.ai."
            )
        client, params, expiry, attempts = entry

        presented = await anyio.to_thread.run_sync(self._kdf, password, limiter=self._kdf_limiter)
        if not hmac.compare_digest(presented, self.__pw_digest):
            self._record_failure(ip)
            attempts += 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                self._pending.pop(sid, None)
                return self._render_form(
                    "", error="Too many failed attempts. Restart the connection from claude.ai."
                )
            self._pending[sid] = _Pending(client, params, expiry, attempts)
            return self._render_form(
                sid, redirect_uri=str(params.redirect_uri), error="Incorrect password."
            )

        # Success: single-use consume, then issue the code via the PARENT (NOT self.authorize
        # → infinite recursion; NOT bare super() → unbound in this handler).
        self._pending.pop(sid, None)
        self._fail_times.pop(ip, None)
        redirect = await InMemoryOAuthProvider.authorize(self, client, params)
        return RedirectResponse(redirect, status_code=302, headers={"Cache-Control": "no-store"})

    def _render_form(
        self, sid: str, *, redirect_uri: str | None = None, error: str = ""
    ) -> HTMLResponse:
        # `sid` on a GET comes from the URL query (attacker-controllable), so it MUST be
        # HTML-escaped before it's reflected into the value="" attribute — otherwise
        # /login?sid="><script>... is a reflected XSS. `redirect_uri` (consent display)
        # and `error` are likewise escaped. create_secure_html_response adds X-Frame-
        # Options: DENY; we add a strict CSP (no scripts; inline styles only; form posts
        # same-origin) as defense-in-depth so even a reflection slip can't execute script.
        safe_sid = html.escape(sid, quote=True)
        err = f'<p style="color:#c00">{html.escape(error)}</p>' if error else ""
        # Consent line: show where the code will be returned so a rogue registered client
        # is noticeable before the password is entered.
        consent = (
            f"<p>Authorizing a client that returns to <b>{html.escape(redirect_uri)}</b>.</p>"
            if redirect_uri
            else ""
        )
        body = (
            "<h2>NotebookLM connector</h2>"
            "<p>Enter the connector password to authorize this client.</p>"
            f"{consent}{err}"
            '<form method="post" action="login">'
            f'<input type="hidden" name="sid" value="{safe_sid}">'
            '<input type="password" name="password" autofocus required '
            'style="font-size:1.1em;padding:.4em;width:20em">'
            '<button type="submit" style="font-size:1.1em;padding:.4em 1em;margin-left:.5em">Sign in</button>'
            "</form>"
        )
        status = 401 if error else 200
        resp = create_secure_html_response(body, status_code=status)
        # NO `form-action` directive on purpose: a correct password POST returns a 302 to
        # the client's (SDK-validated) redirect_uri — e.g. https://claude.ai/... — and
        # browsers apply `form-action` to redirects that result from a form submission, so
        # `form-action 'self'` would SILENTLY BLOCK that cross-origin callback (the login
        # appears to do nothing). The page loads no script (default-src 'none') and reflects
        # nothing unescaped, so the form can only do what this server-rendered HTML says.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'"
        )
        return resp

    # -- throttle --------------------------------------------------------------
    def _throttled(self, ip: str) -> int | None:
        now = time.time()
        times = [t for t in self._fail_times.get(ip, []) if now - t < THROTTLE_WINDOW_SECONDS]
        if times:
            self._fail_times[ip] = times
        else:
            self._fail_times.pop(ip, None)  # never retain an empty list (bound memory)
        if len(times) >= THROTTLE_MAX_FAILURES:
            # retry once the oldest failure ages out of the window (times are appended
            # chronologically, so min == oldest); min() also sidesteps the ADR-0011
            # single-level-positional-index guardrail that a literal `times[0]` trips.
            return int(THROTTLE_WINDOW_SECONDS - (now - min(times))) + 1
        return None

    def _record_failure(self, ip: str) -> None:
        # The public /login POST is pre-auth, so _fail_times is attacker-reachable: cap the
        # number of tracked IPs (evict an arbitrary existing entry when a NEW IP arrives at
        # the cap) so it can't grow unbounded.
        if ip not in self._fail_times and len(self._fail_times) >= MAX_THROTTLE_IPS:
            self._fail_times.pop(next(iter(self._fail_times)), None)
        self._fail_times.setdefault(ip, []).append(time.time())

    def _prune_pending(self) -> None:
        now = time.time()
        for sid in [s for s, (_, _, exp, _) in self._pending.items() if exp < now]:
            self._pending.pop(sid, None)

    # -- persistence (thin wrappers + atomic file) -----------------------------
    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        token = await super().exchange_authorization_code(client, authorization_code)
        await self._save_state()
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        token = await super().exchange_refresh_token(client, refresh_token, scopes)
        await self._save_state()
        return token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await super().revoke_token(token)
        await self._save_state()

    async def _save_state(self) -> None:
        if self._state_path is None:
            return
        # Serialize saves so the snapshot order equals the on-disk write order
        # (see ``self._save_lock``). The lock is held across BOTH the on-loop
        # snapshot and the offloaded write, so a later save cannot snapshot
        # until the earlier save's fsync has completed — the last save started
        # always wins, and no older snapshot can clobber a newer one.
        async with self._save_lock:
            # Build the snapshot dict ON the event loop, BEFORE offloading, so
            # we never iterate the live ``clients`` / token maps from the worker
            # thread while a concurrent coroutine mutates them (the model_dump
            # calls only read loop-owned state here). The blocking mkdir +
            # atomic_write_json (os.fsync under filelock) then runs OFF the loop.
            data: dict[str, Any] = {
                "clients": {k: v.model_dump(mode="json") for k, v in self.clients.items()},
                "access_tokens": {
                    k: v.model_dump(mode="json") for k, v in self.access_tokens.items()
                },
                "refresh_tokens": {
                    k: v.model_dump(mode="json") for k, v in self.refresh_tokens.items()
                },
                "a2r": dict(self._access_to_refresh_map),
                "r2a": dict(self._refresh_to_access_map),
            }
            await anyio.to_thread.run_sync(self._write_state_file, data)

    def _write_state_file(self, data: dict[str, Any]) -> None:
        """Blocking mkdir + atomic write. Runs OFF the event loop via a worker.

        Kept separate from :meth:`_save_state` so the fsync-under-filelock work
        never blocks the loop (see :meth:`_save_state`). ``self._state_path`` is
        non-``None`` here (guarded by the caller).
        """
        assert self._state_path is not None
        try:
            # Persistence is on by default now (#1765) and this dir may be created here
            # before any `login`, so create it 0700 (like get_profile_dir) — a full-account
            # secret must not be group/other-listable. `mode=` secures it AT creation, with
            # no create→chmod TOCTOU window. We deliberately do NOT chmod an existing dir:
            # on a shared/bind-mounted NOTEBOOKLM_HOME the server may not own it, and a
            # failed chmod inside this try would abort the write and drop state on restart.
            parent = self._state_path.parent
            if os.name == "posix":
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._state_path, data)  # POSIX-atomic, 0600, filelock
        except OSError as exc:  # disk error must not crash an active server
            logger.warning("Could not persist OAuth state to %s: %s", self._state_path, exc)

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("oauth_state.json top level is not an object")
            # Build into locals FIRST so a failure partway through doesn't leave a
            # half-applied state (e.g. clients loaded but tokens empty) — assign all or nothing.
            clients = {
                k: OAuthClientInformationFull.model_validate(v)
                for k, v in data.get("clients", {}).items()
            }
            access_tokens = {
                k: AccessToken.model_validate(v) for k, v in data.get("access_tokens", {}).items()
            }
            refresh_tokens = {
                k: RefreshToken.model_validate(v) for k, v in data.get("refresh_tokens", {}).items()
            }
            a2r = dict(data.get("a2r", {}))
            r2a = dict(data.get("r2a", {}))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # A malformed/truncated/wrong-shape file must NOT be a hard startup failure
            # (a valid-JSON non-dict makes `.get`/`.items` raise AttributeError/TypeError;
            # ValidationError ⊂ ValueError) — just start empty (re-register + re-login).
            logger.warning(
                "Ignoring unreadable OAuth state %s (re-auth required): %s", self._state_path, exc
            )
            return
        # All parsed cleanly → apply atomically.
        self.clients, self.access_tokens, self.refresh_tokens = (
            clients,
            access_tokens,
            refresh_tokens,
        )
        self._access_to_refresh_map, self._refresh_to_access_map = a2r, r2a

    def __repr__(self) -> str:  # never surface the password digest
        return f"{type(self).__name__}(base_url={self.base_url!r}, clients={len(self.clients)})"


def build_oauth_provider(config: OAuthConfig) -> AuthProvider:
    """Build the self-hosted OAuth provider from validated config.

    Runs once at HTTP-server startup. ``config.state_path`` is the deployment-scoped,
    base_url-keyed path resolved by :func:`get_oauth_config`, so issued tokens +
    registered clients persist (0600) across restarts and survive a served-account
    switch. Before constructing the provider we migrate a legacy profile-dir
    ``oauth_state.json`` once (:func:`_migrate_legacy_state`) so existing deployments
    keep their registered clients across the #1765 reversal."""
    if config.state_path is not None:
        _migrate_legacy_state(config.state_path, config.legacy_state_path)
        logger.info("OAuth client registry + tokens persist at %s", config.state_path)
    return SelfHostedOAuthProvider(
        password=config.password,
        base_url=config.base_url,
        state_path=config.state_path,
        trust_proxy=config.trust_proxy,
    )
