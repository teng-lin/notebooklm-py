"""Application-layer Playwright account repair composition."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeAlias

import httpx

from .account_types import Account, PlaywrightAccountRepairResult
from .profile_account import ProfileAccount
from .profile_migration import AccountMetadataWriter, LegacyAccountMigrator
from .profile_store import ProfileStore

logger = logging.getLogger("notebooklm.auth")

PokeSession: TypeAlias = Callable[[httpx.AsyncClient, Path | None], Awaitable[None]]
LoadCookieJar: TypeAlias = Callable[[Path], httpx.Cookies]
AccountEnumerator: TypeAlias = Callable[[httpx.Cookies, PokeSession], Awaitable[list[Account]]]
AccountSelector: TypeAlias = Callable[
    [list[Account], str | None], tuple[Account | None, str | None]
]
ActiveEmailExtractor: TypeAlias = Callable[[str], str | None]
WriterFactory: TypeAlias = Callable[[Path], AccountMetadataWriter]


class AccountRepairService:
    """Perform one account repair with explicit operation collaborators."""

    def __init__(
        self,
        *,
        writer_factory: WriterFactory,
        load_cookie_jar: LoadCookieJar,
        enumerate_accounts: AccountEnumerator,
        select_account: AccountSelector,
        extract_active_email: ActiveEmailExtractor,
        poke_session: PokeSession,
    ) -> None:
        self._claim_lock = threading.Lock()
        self._claimed = False
        self._writer_factory = writer_factory
        self._load_cookie_jar = load_cookie_jar
        self._enumerate_accounts = enumerate_accounts
        self._select_account = select_account
        self._extract_active_email = extract_active_email
        self._poke_session = poke_session

    async def repair(
        self,
        storage_path: Path,
        *,
        page_html: str | None = None,
    ) -> PlaywrightAccountRepairResult:
        """Repair account metadata once and project handled failures as values."""
        with self._claim_lock:
            if self._claimed:
                raise RuntimeError("AccountRepairService.repair() may only be called once")
            self._claimed = True
            writer_factory = self._writer_factory
            load_cookie_jar = self._load_cookie_jar
            enumerate_accounts = self._enumerate_accounts
            select_account = self._select_account
            extract_active_email = self._extract_active_email
            poke_session = self._poke_session

        try:
            active_email = extract_active_email(page_html) if isinstance(page_html, str) else None
            try:
                jar = await asyncio.to_thread(load_cookie_jar, storage_path)
                accounts = await enumerate_accounts(jar, poke_session)
                selected, reason = select_account(accounts, active_email)
                if selected is None:
                    writer_factory(storage_path).clear()
                    return PlaywrightAccountRepairResult(
                        written=False,
                        ambiguity_reason=reason,
                    )
                writer_factory(storage_path).write(
                    ProfileAccount(authuser=selected.authuser, email=selected.email)
                )
                return PlaywrightAccountRepairResult(written=True, email=selected.email)
            except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
                try:
                    writer_factory(storage_path).clear()
                except Exception as clear_exc:  # noqa: BLE001 - best-effort cleanup
                    logger.warning(
                        "Failed to clear stale account metadata for %s: %s",
                        storage_path,
                        clear_exc,
                    )
                return PlaywrightAccountRepairResult(written=False, error=str(exc))
        finally:
            del self._writer_factory
            del self._load_cookie_jar
            del self._enumerate_accounts
            del self._select_account
            del self._extract_active_email
            del self._poke_session
            del writer_factory
            del load_cookie_jar
            del enumerate_accounts
            del select_account
            del extract_active_email
            del poke_session


def _compose_account_repair_service(
    *,
    enumerate_accounts: AccountEnumerator,
    select_account: AccountSelector,
    extract_active_email: ActiveEmailExtractor,
) -> AccountRepairService:
    """Compose one repair service from canonical call-time collaborators."""
    from .cookies import build_httpx_cookies_from_storage
    from .keepalive import _poke_session

    def _new_writer(storage_path: Path) -> AccountMetadataWriter:
        return AccountMetadataWriter(ProfileStore(storage_path), LegacyAccountMigrator())

    return AccountRepairService(
        writer_factory=_new_writer,
        load_cookie_jar=build_httpx_cookies_from_storage,
        enumerate_accounts=enumerate_accounts,
        select_account=select_account,
        extract_active_email=extract_active_email,
        poke_session=_poke_session,
    )
