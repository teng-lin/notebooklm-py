"""Stateless master-token exchange and cookie-minting network service."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..exceptions import MissingDependencyError
from .master_token_types import MasterToken

_MASTER_APP = "com.google.android.apps.chromecast.app"
_MASTER_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"
_OAUTHLOGIN_SERVICE = "oauth2:https://www.google.com/accounts/OAuthLogin"
_REQUIRED_MINTED_COOKIES = {"SID", "APISID", "SAPISID"}

KEEPALIVE_ROTATE_URL = "https://accounts.google.com/RotateCookies"
_KEEPALIVE_ROTATE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://accounts.google.com",
}
_KEEPALIVE_ROTATE_BODY = '[000,"-0000000000000000000"]'
_KEEPALIVE_POKE_TIMEOUT = 15.0
_ROTATE_POST_KWARGS: dict[str, Any] = {
    "headers": _KEEPALIVE_ROTATE_HEADERS,
    "content": _KEEPALIVE_ROTATE_BODY,
    "follow_redirects": True,
    "timeout": _KEEPALIVE_POKE_TIMEOUT,
}

logger = logging.getLogger("notebooklm.auth.master_token")

# Serializes the temporary mutation of third-party logger levels across
# simultaneous exchange/mint attempts. It owns no credential or request state.
_LOG_LOCK = threading.Lock()


class _MintError(Exception):
    pass


class OAuthMintError(_MintError):
    """A sanitized failure from the durable-token OAuth mint boundary."""


@dataclass(frozen=True)
class OAuthClientSpec:
    """Immutable Google OAuth client identity supplied by a protocol adapter."""

    service: str
    app: str
    client_sig: str


@dataclass(frozen=True)
class MintedOAuthToken:
    """Short-lived OAuth credential plus an optional server-owned expiry."""

    token: str = field(repr=False)
    expires_at: int | None


_CHROMECAST_OAUTH_SPEC = OAuthClientSpec(
    service=_OAUTHLOGIN_SERVICE,
    app=_MASTER_APP,
    client_sig=_MASTER_SIG,
)


def _require_gpsoauth() -> Any:
    try:
        import gpsoauth  # noqa: PLC0415 (lazy optional [headless] dependency)
    except ImportError as exc:  # pragma: no cover - import guard
        raise MissingDependencyError(
            "Master-token auth needs gpsoauth. Install: pip install 'notebooklm-py[headless]'"
        ) from exc
    return gpsoauth


@contextmanager
def _quiet_gpsoauth_logging() -> Iterator[None]:
    """Suppress request-body logging only while one gpsoauth call runs."""
    names = ("urllib3", "requests", "urllib3.connectionpool")
    with _LOG_LOCK:
        saved = {name: logging.getLogger(name).level for name in names}
        try:
            for name in names:
                logging.getLogger(name).setLevel(logging.WARNING)
            yield
        finally:
            for name, level in saved.items():
                logging.getLogger(name).setLevel(level)


def _perform_oauth(
    gpsoauth: Any,
    email: str,
    master_token: str,
    android_id: str,
    spec: OAuthClientSpec,
) -> Any:
    """Run the sole blocking mint exchange under logger suppression."""
    with _quiet_gpsoauth_logging():
        return gpsoauth.perform_oauth(
            email,
            master_token,
            android_id,
            service=spec.service,
            app=spec.app,
            client_sig=spec.client_sig,
        )


def _parse_oauth_expiry(value: Any) -> int | None:
    """Parse the server's optional Unix-seconds expiry without guessing one."""
    if (
        not isinstance(value, str)
        or len(value) > 10
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    try:
        return int(value)
    except ValueError:
        # Defensive backstop for interpreter-specific integer conversion.
        return None


async def _rotate_post(client: httpx.AsyncClient) -> httpx.Response:
    """Send the one async RotateCookies wire contract and require success."""
    response = await client.post(KEEPALIVE_ROTATE_URL, **_ROTATE_POST_KWARGS)
    response.raise_for_status()
    return response


def _rotate_post_sync(client: httpx.Client) -> httpx.Response:
    """Synchronous twin of :func:`_rotate_post` for recovery clients."""
    response = client.post(KEEPALIVE_ROTATE_URL, **_ROTATE_POST_KWARGS)
    response.raise_for_status()
    return response


class MintService:
    """Perform one stateless exchange or cookie-minting attempt."""

    def exchange(
        self,
        email: str,
        oauth_token: str,
        android_id: str,
    ) -> MasterToken:
        """Exchange a single-use OAuth token for a durable master token."""
        try:
            gpsoauth = _require_gpsoauth()
            try:
                with _quiet_gpsoauth_logging():
                    result = gpsoauth.exchange_token(email, oauth_token, android_id)
            except Exception as exc:  # noqa: BLE001 (sanitize dependency/transport failures)
                raise _MintError("exchange_token failed (network or gpsoauth error).") from exc
            token = result.get("Token")
            if not token:
                raise _MintError(
                    "exchange_token rejected the oauth_token "
                    f"(Error={result.get('Error', 'unknown')}). The oauth_token is "
                    "single-use and short-lived — re-capture it."
                )
            return MasterToken(email=email, android_id=android_id, secret=str(token))
        finally:
            # Dependency, transport and process-exit paths all leave this frame
            # without the caller's single-use token.
            del oauth_token

    async def mint_oauth(
        self,
        master_token: MasterToken,
        spec: OAuthClientSpec,
    ) -> MintedOAuthToken:
        """Mint one short-lived OAuth token for an immutable client identity."""
        oauth: Any = None
        bearer: Any = None
        minted_token: str | None = None
        expires_at: int | None = None
        failure_message: str | None = None
        try:
            gpsoauth = _require_gpsoauth()
            try:
                oauth = await asyncio.to_thread(
                    _perform_oauth,
                    gpsoauth,
                    master_token.email,
                    master_token.secret,
                    master_token.android_id,
                    spec,
                )
            except Exception:  # noqa: BLE001 (discard dependency/transport exception + traceback)
                failure_message = "perform_oauth failed (network or gpsoauth error)."
            else:
                try:
                    bearer = oauth.get("Auth")
                    if bearer:
                        minted_token = str(bearer)
                        expires_at = _parse_oauth_expiry(oauth.get("Expiry"))
                    else:
                        failure_message = (
                            "perform_oauth rejected the master token. "
                            "Re-bootstrap with `notebooklm login --master-token`."
                        )
                except Exception:  # noqa: BLE001 (sanitize malformed dependency response)
                    failure_message = "perform_oauth returned a malformed response."

            if minted_token is not None:
                return MintedOAuthToken(token=minted_token, expires_at=expires_at)

            # Raise only after the dependency exception/parser frame has unwound.
            # The explicit chain reset also prevents an active caller exception
            # from becoming an implicit, potentially secret-bearing context.
            error = OAuthMintError(
                failure_message or "perform_oauth returned a malformed response."
            )
            try:
                raise error
            except OAuthMintError:
                error.__cause__ = None
                error.__context__ = None
                error.__suppress_context__ = False
                raise
        finally:
            # Every exit, including cancellation and process-exit signals,
            # scrubs raw credential carriers before this frame can escape.
            del master_token, oauth, bearer, minted_token

    async def mint(self, token: MasterToken) -> httpx.Cookies:
        """Mint a fresh live transport jar from one durable master token."""
        try:
            oauth = await self.mint_oauth(token, _CHROMECAST_OAUTH_SPEC)
        except BaseException:
            # ``mint_oauth`` owns error typing and sanitization. Preserve that
            # identity while removing the durable token from this adapter frame.
            del token
            raise
        bearer = oauth.token

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                authorization = {"Authorization": f"Bearer {bearer}"}
                oauth_login = await client.get(
                    "https://accounts.google.com/OAuthLogin",
                    params={"source": "ChromiumBrowser", "issueuberauth": "1"},
                    headers=authorization,
                )
                uberauth = oauth_login.text.strip()
                if oauth_login.status_code != 200 or not uberauth or " " in uberauth:
                    raise _MintError("OAuthLogin did not return a uberauth token.")
                await client.get(
                    "https://accounts.google.com/MergeSession",
                    params={
                        "service": "mail",
                        "continue": "https://www.google.com",
                        "uberauth": uberauth,
                    },
                    headers=authorization,
                )
                try:
                    await _rotate_post(client)
                except httpx.HTTPError as exc:
                    exception_name = type(exc).__name__
                    status_code = (
                        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    )
                    logger.debug(
                        "RotateCookies during mint failed (non-fatal): %s status=%s",
                        exception_name,
                        status_code,
                    )
                jar = httpx.Cookies()
                for cookie in client.cookies.jar:
                    jar.jar.set_cookie(cookie)
        except httpx.HTTPError:
            raise _MintError(
                "cookie minting failed (network error reaching accounts.google.com)."
            ) from None

        names = {cookie.name for cookie in jar.jar}
        missing = _REQUIRED_MINTED_COOKIES - names
        if missing:
            raise _MintError(
                f"Minted cookie jar is missing required cookies: {sorted(missing)}. "
                "MergeSession may have changed; the session would fail PSIDTS recovery."
            )
        return jar
