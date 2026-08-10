"""Generation-safe account-email resolution for long-lived clients."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .cookie_types import CookieIdentity, CookieJar
from .profile_account import AccountView, ProfileAccount, parse_profile_account
from .profile_document import ProfileDocument
from .profile_migration import (
    LegacyAccountContext,
    _in_band_route,
)
from .profile_store import ProfileStore
from .tokens import AuthTokens

logger = logging.getLogger("notebooklm.auth")

AccountEmailCacheKey = tuple[int, int, str | None]
AccountEmailProbe = Callable[[httpx.AsyncClient, int], Awaitable[str | None]]
ToThread = Callable[..., Awaitable[object]]


def _session_key(auth: AuthTokens) -> AccountEmailCacheKey:
    return (
        auth._profile_session_generation,
        auth.authuser,
        auth.account_email,
    )


def _auth_material_values(jar: CookieJar) -> dict[CookieIdentity, str]:
    return {identity: cookie.value for identity, cookie in jar._auth_material_state().items()}


def _read_matching_account_heal_document(
    storage_path: Path,
    *,
    expected_cookies: CookieJar,
    expected_authuser: int,
) -> tuple[ProfileDocument, str | None] | None:
    """Return a self-heal sample only when disk still describes the live session."""
    store = ProfileStore(storage_path)
    document = store.read_document()
    if _auth_material_values(document.cookies()) != _auth_material_values(expected_cookies):
        return None
    first_payload = document.to_json()
    route_present, account = _in_band_route(first_payload)
    if not route_present:
        legacy_account = LegacyAccountContext().read(storage_path)
        second = store.read_document()
        second_payload = second.to_json()
        if _auth_material_values(second.cookies()) != _auth_material_values(expected_cookies):
            return None
        route_present, account = _in_band_route(second_payload)
        if not route_present:
            account = (
                None
                if second_payload != first_payload
                else parse_profile_account(legacy_account, view=AccountView.ROUTE)
            )
        document = second
    stored_authuser = 0 if account is None else account.authuser
    if stored_authuser != expected_authuser:
        return None
    return document, None if account is None else account.email


def _write_account_metadata_if_document_unchanged(
    storage_path: Path,
    *,
    expected: ProfileDocument,
    authuser: int,
    email: str,
) -> bool:
    """Self-heal account metadata only if the probed profile has not advanced."""
    store = ProfileStore(storage_path)
    applied = store._update_account_if_document_unchanged(
        ProfileAccount(authuser=authuser, email=email),
        expected=expected,
    )
    if applied:
        LegacyAccountContext().scrub(storage_path)
    return applied


async def resolve_account_email(
    *,
    auth: AuthTokens,
    cached_email: str | None,
    cached_key: AccountEmailCacheKey | None,
    live_fallback: bool,
    get_http_client: Callable[[], httpx.AsyncClient],
    probe: AccountEmailProbe,
    to_thread: ToThread = asyncio.to_thread,
) -> tuple[str | None, str | None, AccountEmailCacheKey]:
    """Resolve identity without crossing stored/live profile generations.

    The live probe and its best-effort persistence both await. Re-resolve once
    if profile recovery advances the paired cookie/route generation across
    either boundary; an old result must never be returned, cached, or written
    into the newly selected profile.
    """
    for _attempt in range(2):
        session_key = _session_key(auth)
        if cached_key != session_key:
            cached_email = None
            cached_key = session_key
        if cached_email is not None:
            return cached_email or None, cached_email, cached_key

        email = auth.account_email
        storage_path = auth.storage_path
        expected_document = None
        live_cookies = auth.cookie_jar
        try:
            live_cookies = get_http_client().cookies
        except RuntimeError:
            # Preserve the documented network-free, outside-context lookup.
            # The live-fallback probe below still surfaces a closed client.
            pass
        if not email and storage_path is not None and live_cookies is not None:
            try:
                matched = _read_matching_account_heal_document(
                    storage_path,
                    expected_cookies=CookieJar.from_httpx(live_cookies),
                    expected_authuser=auth.authuser,
                )
            except (OSError, UnicodeError, ValueError, RuntimeError):
                matched = None
            if matched is not None:
                expected_document, email = matched
        if email:
            return email, email, session_key
        if not live_fallback:
            return None, cached_email, session_key

        authuser = auth.authuser
        try:
            email = await probe(get_http_client(), authuser)
        except httpx.HTTPError as exc:
            if session_key != _session_key(auth):
                continue
            logger.debug("account-email live probe failed: %s", type(exc).__name__)
            return None, cached_email, session_key
        if session_key != _session_key(auth):
            continue
        if not email:
            return None, cached_email, session_key

        if storage_path is not None and expected_document is not None:
            try:
                await to_thread(
                    _write_account_metadata_if_document_unchanged,
                    storage_path,
                    expected=expected_document,
                    authuser=authuser,
                    email=email,
                )
            except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                logger.debug("account-email self-heal write failed: %s", type(exc).__name__)
        if session_key != _session_key(auth):
            continue
        return email, email, session_key

    # Continuous profile churn is bounded. Never return a probe result from an
    # obsolete session; a currently in-memory identity remains safe.
    current_key = _session_key(auth)
    return auth.account_email, auth.account_email, current_key
