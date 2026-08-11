"""Path-owned master-token bootstrap composition."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
from filelock import FileLock, Timeout

from ..paths import master_token_path_for
from .cookie_semantics import cookie_is_http_only
from .cookie_types import Cookie, CookieJar
from .master_token_types import MasterToken, _MasterTokenRecordError
from .mint_service import MintService, _MintError
from .profile_store import (
    MintedSessionWriteRequest,
    ProfileStore,
    _MintedSessionOwnershipRefused,
)

logger = logging.getLogger("notebooklm.auth.master_token")


class _BootstrapError(Exception):
    pass


class BootstrapOutcome(enum.Enum):
    """Resolved state of one missing-storage bootstrap attempt."""

    MINTED = "minted"
    PRESENT_AFTER_WAIT = "present_after_wait"
    PRESENT_ON_ENTRY = "present_on_entry"
    NO_TOKEN = "no_token"


class MasterTokenBootstrapper:
    """Coordinate master-token bootstrap over one authoritative profile store."""

    def __init__(
        self,
        *,
        mint_service: MintService,
        store: ProfileStore,
        bootstrap_lock: FileLock,
        verifier: Callable[[Path], Awaitable[int]],
    ) -> None:
        self._mint_service = mint_service
        self._store = store
        self._bootstrap_lock = bootstrap_lock
        self._verifier = verifier

    def _read_master_token(self) -> MasterToken | None:
        malformed = False
        try:
            return self._store.read_master_token()
        except (OSError, json.JSONDecodeError) as exc:
            raise _BootstrapError(f"Unreadable master_token.json: {exc}") from exc
        except _MasterTokenRecordError:
            malformed = True
        if malformed:
            raise _BootstrapError("master_token.json is malformed or an unsupported version.")
        raise AssertionError("unreachable")

    def assert_account_writable(
        self,
        *,
        email: str,
        force: bool = False,
        session_owner_reader: Callable[[Path], str | None],
    ) -> None:
        if force:
            return
        try:
            if not email:
                raise _BootstrapError("assert_account_writable requires a non-empty email.")
            try:
                token = self._read_master_token()
            except _BootstrapError:
                token = None
            token_owner = token.email if token is not None else None
            del token
            session_owner = session_owner_reader(self._store.path)
            owners = {
                owner.strip()
                for owner in (session_owner, token_owner)
                if isinstance(owner, str) and owner.strip()
            }
            conflict = next(
                (owner for owner in owners if owner.casefold() != email.casefold()), None
            )
            if conflict:
                raise _BootstrapError(
                    f"This profile already belongs to {conflict}, but --account is {email}. "
                    f"Minting here would overwrite {conflict}'s session and master token. "
                    "Use a dedicated profile (e.g. `notebooklm -p <name> login --master-token "
                    f"--account {email}`), or pass --force to overwrite this one."
                )
        finally:
            del session_owner_reader

    def _resolve_android_id(
        self,
        *,
        explicit: str | None,
        android_id_generator: Callable[[], str],
    ) -> str:
        try:
            token = self._read_master_token()
            if explicit:
                return explicit
            if token is not None:
                return token.android_id
            return android_id_generator()
        finally:
            del android_id_generator

    def _exchange(self, email: str, oauth_token: str, android_id: str) -> MasterToken:
        try:
            caller_exception = sys.exc_info()[1]
            try:
                token = self._mint_service.exchange(email, oauth_token, android_id)
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
                return token
            caller_exception = None
            error = _BootstrapError(error_snapshot[0])
            try:
                raise error
            except _BootstrapError:
                error.__cause__ = error_snapshot[1]
                error.__context__ = error_snapshot[2]
                error.__suppress_context__ = error_snapshot[3]
                raise
        finally:
            caller_exception = None
            del oauth_token

    async def _mint(self, token: MasterToken) -> httpx.Cookies:
        try:
            caller_exception = sys.exc_info()[1]
            try:
                jar = await self._mint_service.mint(token)
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
            error = _BootstrapError(error_snapshot[0])
            try:
                raise error
            except _BootstrapError:
                error.__cause__ = error_snapshot[1]
                error.__context__ = error_snapshot[2]
                error.__suppress_context__ = error_snapshot[3]
                raise
        finally:
            caller_exception = None
            del token

    @staticmethod
    def _minted_session_request(
        jar: httpx.Cookies,
        *,
        email: str | None,
        force: bool = False,
        refuse_unknown_owner: bool = True,
    ) -> MintedSessionWriteRequest:
        try:
            cookies = CookieJar(
                Cookie(
                    name=cookie.name,
                    value=cast(str, cookie.value),
                    domain=cookie.domain,
                    path=cookie.path or "/",
                    expires=cookie.expires,
                    http_only=cookie_is_http_only(cookie),
                    secure=cookie.secure,
                    same_site="None",
                )
                for cookie in jar.jar
            )
            return MintedSessionWriteRequest(cookies, email, force, refuse_unknown_owner)
        finally:
            del jar

    def _replace_minted_session(self, request: MintedSessionWriteRequest) -> None:
        refusal_message: str | None = None
        try:
            try:
                self._store.replace_minted_session(request)
            except _MintedSessionOwnershipRefused as exc:
                refusal_message = str(exc)
        finally:
            del request
        if refusal_message is not None:
            error = _BootstrapError(refusal_message)
            try:
                raise error
            except _BootstrapError:
                error.__cause__ = None
                error.__context__ = None
                error.__suppress_context__ = False
                raise

    async def bootstrap_from_oauth_token(
        self,
        *,
        email: str,
        oauth_token: str,
        android_id: str | None = None,
        verify: bool = True,
        force: bool = False,
        session_owner_reader: Callable[[Path], str | None],
        android_id_generator: Callable[[], str],
    ) -> int:
        try:
            _token_path = master_token_path_for(self._store.path)
            aid = await asyncio.to_thread(
                self._resolve_android_id,
                explicit=android_id,
                android_id_generator=android_id_generator,
            )
            await asyncio.to_thread(
                self.assert_account_writable,
                email=email,
                force=force,
                session_owner_reader=session_owner_reader,
            )
            token = await asyncio.to_thread(self._exchange, email, oauth_token, aid)
            try:
                jar = await self._mint(token)
                request = self._minted_session_request(jar, email=email, force=force)
                del jar
                try:
                    await asyncio.to_thread(self._replace_minted_session, request)
                finally:
                    del request
                await asyncio.to_thread(self._store.write_master_token, token)
            finally:
                del token
            return await self._verifier(self._store.path) if verify else -1
        finally:
            del oauth_token, session_owner_reader, android_id_generator

    async def remint_from_stored_token(
        self,
        *,
        strict_loader: Callable[[Path], httpx.Cookies],
    ) -> httpx.Cookies:
        try:
            token_path = master_token_path_for(self._store.path)
            token = await asyncio.to_thread(self._read_master_token)
            if token is None:
                raise _BootstrapError(
                    f"No master token at {token_path}. Run `notebooklm login --master-token` first."
                )
            try:
                jar = await self._mint(token)
                request = self._minted_session_request(
                    jar,
                    email=token.email,
                    refuse_unknown_owner=False,
                )
                del jar
                try:
                    await asyncio.to_thread(self._replace_minted_session, request)
                finally:
                    del request
            finally:
                del token

            try:
                return await asyncio.to_thread(strict_loader, self._store.path)
            except (OSError, ValueError) as exc:
                raise _BootstrapError(
                    "Master token re-mint persisted a session but reloading it failed "
                    f"({type(exc).__name__}): {exc}"
                ) from exc
        finally:
            del strict_loader

    async def _acquire_bootstrap_lock(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90.0
        while True:
            try:
                self._bootstrap_lock.acquire(blocking=False)
            except Timeout as exc:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise _BootstrapError(
                        "Timed out waiting for another process to finish master-token bootstrap."
                    ) from exc
                await asyncio.sleep(min(0.05, remaining))
            else:
                return

    async def _run_remint_to_settlement(
        self,
        *,
        strict_loader: Callable[[Path], httpx.Cookies],
    ) -> None:
        task = asyncio.create_task(self.remint_from_stored_token(strict_loader=strict_loader))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
        finally:
            del strict_loader

    async def bootstrap_storage(
        self,
        *,
        strict_loader: Callable[[Path], httpx.Cookies],
    ) -> BootstrapOutcome:
        try:
            storage_path = self._store.path
            if storage_path.exists():
                outcome = BootstrapOutcome.PRESENT_ON_ENTRY
            else:
                token_path = master_token_path_for(storage_path)
                if not token_path.exists():
                    outcome = BootstrapOutcome.NO_TOKEN
                else:
                    await self._acquire_bootstrap_lock()
                    try:
                        if storage_path.exists():
                            outcome = BootstrapOutcome.PRESENT_AFTER_WAIT
                        elif not token_path.exists():
                            outcome = BootstrapOutcome.NO_TOKEN
                        else:
                            await self._run_remint_to_settlement(strict_loader=strict_loader)
                            outcome = BootstrapOutcome.MINTED
                    finally:
                        self._bootstrap_lock.release()
            logger.debug("bootstrap_storage_from_master_token(%s) -> %s", storage_path, outcome)
            return outcome
        finally:
            del strict_loader
