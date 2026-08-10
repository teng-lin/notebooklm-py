"""Stateless master-token exchange and cookie-minting network service."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

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


def _require_gpsoauth() -> Any:
    try:
        import gpsoauth  # noqa: PLC0415 (lazy optional [headless] dependency)
    except ImportError as exc:  # pragma: no cover - import guard
        raise _MintError(
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
) -> Any:
    """Run the sole blocking mint exchange under logger suppression."""
    with _quiet_gpsoauth_logging():
        return gpsoauth.perform_oauth(
            email,
            master_token,
            android_id,
            service=_OAUTHLOGIN_SERVICE,
            app=_MASTER_APP,
            client_sig=_MASTER_SIG,
        )


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
        gpsoauth = _require_gpsoauth()
        try:
            with _quiet_gpsoauth_logging():
                result = gpsoauth.exchange_token(email, oauth_token, android_id)
        except Exception as exc:  # noqa: BLE001 (sanitize dependency/transport failures)
            raise _MintError("exchange_token failed (network or gpsoauth error).") from exc
        token = result.get("Token")
        if not token:
            raise _MintError(
                f"exchange_token rejected the oauth_token (Error={result.get('Error', 'unknown')}). "
                "The oauth_token is single-use and short-lived — re-capture it."
            )
        return MasterToken(email=email, android_id=android_id, secret=str(token))

    async def mint(self, token: MasterToken) -> httpx.Cookies:
        """Mint a fresh live transport jar from one durable master token."""
        gpsoauth = _require_gpsoauth()
        try:
            oauth = await asyncio.to_thread(
                _perform_oauth,
                gpsoauth,
                token.email,
                token.secret,
                token.android_id,
            )
        except Exception as exc:  # noqa: BLE001 (sanitize dependency/transport failures)
            raise _MintError("perform_oauth failed (network or gpsoauth error).") from exc
        bearer = oauth.get("Auth")
        if not bearer:
            raise _MintError(
                f"perform_oauth rejected the master token (Error={oauth.get('Error', 'unknown')}). "
                "Re-bootstrap with `notebooklm login --master-token`."
            )

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
