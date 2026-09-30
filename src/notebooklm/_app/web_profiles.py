"""Web-backend multi-profile admission, construction, and diagnostics.

Serving several Web profiles from one process is safe only when each profile
owns its own Google Web session. Profiles may hold copies of one
``master_token.json`` (each mints its own session), but two profiles holding
copies of one ``storage_state.json`` would drive a single cookie session from
two independent clients, each rotating and persisting it. Admission therefore
refuses a profile whose ``__Secure-1PSID`` matches a sibling's.

Session identity is compared through a keyed digest under a per-process random
salt, so neither the cookie value nor its digest is logged, persisted, or
returned to a caller. Admission reads files only; it never contacts Google.
It runs whenever a profile's client opens (startup or recovery), so a copy
made after both profiles already serve is not detected until one reopens.

REST and MCP construct one :class:`WebProfileSet` per lifespan and hold its
:meth:`WebProfileSet.open_turn` around each bounded client open, so admission,
cold bootstrap, and the session open run one profile at a time without the
wait counting against a profile's startup deadline.

This module is transport-neutral and depends only on the public ``notebooklm``
surface (``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .. import auth
from ..client import NotebookLMClient
from ..exceptions import ConfigurationError
from .auth_check import AuthCheckPlan, run_auth_check
from .client_config import adapter_client_config
from .master_token import (
    bootstrap_missing_storage_from_master_token,
    inspect_master_token_status,
)

__all__ = [
    "WebProfileHealth",
    "WebProfileSet",
    "WebProfileUnavailable",
    "WebSessionConflict",
    "refuse_web_multi_profile_environment",
    "web_session_key",
]

logger = logging.getLogger(__name__)

AUTH_JSON_ENV = "NOTEBOOKLM_AUTH_JSON"
HEADLESS_REAUTH_CDP_URL_ENV = "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL"
_SESSION_COOKIE = "__Secure-1PSID"
_SESSION_COOKIE_DOMAIN = ".google.com"
# Per-process: digests are comparable only within one server process and are
# useless as an offline oracle for the cookie value.
_SESSION_KEY_SALT = secrets.token_bytes(32)

AdmitState = Literal["ready", "bootstrap"]


class WebProfileUnavailable(ConfigurationError):
    """A configured Web profile cannot be admitted for serving."""


class WebSessionConflict(WebProfileUnavailable):
    """A Web profile shares its cookie session with another configured profile."""


def refuse_web_multi_profile_environment() -> None:
    """Reject process-wide auth settings that would merge Web profiles.

    Raises:
        ValueError: Inline auth is present, or a shared CDP browser is set for
            headless re-authentication.
    """
    # Presence, not truthiness: the loader treats any set value as inline auth.
    if AUTH_JSON_ENV in os.environ:
        raise ValueError(
            "Multi-profile Web serving refuses NOTEBOOKLM_AUTH_JSON: inline auth is "
            "process-wide and bypasses per-profile storage. Unset it and log in each "
            "profile (notebooklm -p <name> login)."
        )
    if os.environ.get(HEADLESS_REAUTH_CDP_URL_ENV, "").strip():
        raise ValueError(
            "Multi-profile Web serving refuses NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL: one "
            "attached Chrome would re-authenticate every profile into the same browser "
            "session. Unset it; NOTEBOOKLM_HEADLESS_REAUTH=1 uses each profile's own "
            "browser directory."
        )


def web_session_key(storage_path: Path) -> str | None:
    """Return an opaque, process-local identity for a profile's Web session.

    Reads ``storage_state.json`` only. ``__Secure-1PSIDTS`` is deliberately not
    required: the normal load path repairs it.

    Returns:
        ``None`` when the storage file does not exist, else a keyed digest of
        the ``__Secure-1PSID`` cookie (the ``.google.com`` row when several
        exist). Never the cookie value.

    Raises:
        WebProfileUnavailable: The file is unreadable, is not a JSON object, or
            carries no ``__Secure-1PSID`` cookie.
    """
    try:
        raw = storage_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        raise WebProfileUnavailable("Web profile storage is unreadable") from None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        raise WebProfileUnavailable("Web profile storage is not valid JSON") from None
    if not isinstance(state, dict):
        raise WebProfileUnavailable("Web profile storage is not a JSON object")
    fallback: str | None = None
    preferred: str | None = None
    for entry in auth._sanitized_auth_entries(state):
        value = entry["value"]
        if entry["name"] != _SESSION_COOKIE or not isinstance(value, str) or not value:
            continue
        if entry["domain"] == _SESSION_COOKIE_DOMAIN and preferred is None:
            preferred = value
        elif fallback is None:
            fallback = value
    selected = preferred if preferred is not None else fallback
    if selected is None:
        raise WebProfileUnavailable(f"Web profile storage has no {_SESSION_COOKIE} cookie")
    return hmac.new(_SESSION_KEY_SALT, selected.encode("utf-8"), hashlib.sha256).hexdigest()


def _open_web_client(
    storage_path: Path, profile: str, keepalive: float | None
) -> AbstractAsyncContextManager[NotebookLMClient]:
    """Open one profile's Web client from its explicit storage path.

    ``profile`` is forwarded as well so refresh-command environment and token
    routing name the profile whose file is loaded.
    """
    return cast(
        "AbstractAsyncContextManager[NotebookLMClient]",
        NotebookLMClient.from_storage(
            path=str(storage_path),
            profile=profile,
            config=adapter_client_config(backend="web", keepalive=keepalive),
        ),
    )


def _no_env_auth_json() -> str:
    """Inline-auth reader for the neutral auth check; never called (file-only plan)."""
    return ""  # pragma: no cover - unreachable while has_env_auth is False


@dataclass(frozen=True, slots=True)
class WebProfileHealth:
    """File-only diagnostics for one Web profile (no values, no network)."""

    storage_exists: bool
    json_valid: bool
    cookies_present: bool
    sid_cookie: bool
    local_checks_passed: bool
    master_token_present: bool
    session_conflict: bool
    account: Mapping[str, Any] | None


class WebProfileSet:
    """Admit, open, and diagnose a static set of Web profiles for one lifespan.

    Construct it on the serving loop. :meth:`open_turn` serializes opens across
    every profile in the set; :meth:`factory` performs the admission checks and
    opens the client, so callers hold the turn around a bounded ``factory`` call.
    """

    def __init__(self, paths: Mapping[str, Path], *, keepalive: float | None) -> None:
        self._paths = dict(paths)
        self._keepalive = keepalive
        self._loop = asyncio.get_running_loop()
        self._open_lock = asyncio.Lock()

    def _assert_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Web profiles must be used on their owning event loop")

    @asynccontextmanager
    async def open_turn(self) -> AsyncIterator[None]:
        """Hold the process-wide Web open slot for one bounded open attempt."""
        self._assert_loop()
        async with self._open_lock:
            yield

    def _sharing_profiles(self, name: str, key: str) -> list[str]:
        shared: list[str] = []
        for other, path in self._paths.items():
            if other == name:
                continue
            try:
                other_key = web_session_key(path)
            except WebProfileUnavailable:
                # A broken sibling cannot share a readable session; it is
                # diagnosed when that sibling itself opens.
                continue
            if other_key is not None and hmac.compare_digest(other_key, key):
                shared.append(other)
        return shared

    def admit(self, name: str) -> AdmitState:
        """Classify ``name`` from local files and refuse a shared Web session.

        Returns:
            ``"ready"`` when a cookie file is present and not shared, or
            ``"bootstrap"`` when it is absent but a readable master token can
            mint a fresh session.

        Raises:
            WebProfileUnavailable: Neither a usable cookie file nor a readable
                master token is present.
            WebSessionConflict: A sibling holds the same Web session.
        """
        path = self._paths[name]
        key = web_session_key(path)
        if key is None:
            status = inspect_master_token_status(path, has_env_auth=False)
            if not status.present or status.unreadable_error_type is not None:
                raise WebProfileUnavailable(
                    "Web profile requires storage_state.json or a readable master_token.json"
                )
            return "bootstrap"
        shared = self._sharing_profiles(name, key)
        if shared:
            message = (
                f"Web profile {name!r} shares a Web session with configured profile(s) "
                f"{', '.join(repr(other) for other in shared)} (copied storage_state.json); "
                f"refusing it. Log in separately (notebooklm -p {name} login) or delete "
                "the copy and keep master_token.json to mint a fresh session."
            )
            logger.warning("%s", message)
            raise WebSessionConflict(message)
        return "ready"

    @asynccontextmanager
    async def factory(self, name: str) -> AsyncIterator[NotebookLMClient]:
        """Admit ``name``, bootstrap it if needed, and open its Web client."""
        refuse_web_multi_profile_environment()
        path = self._paths[name]
        state = await asyncio.to_thread(self.admit, name)
        if state == "bootstrap":
            await bootstrap_missing_storage_from_master_token(path)
            # Re-admit: the minted session must exist and be distinct.
            if await asyncio.to_thread(self.admit, name) != "ready":
                raise WebProfileUnavailable("Web profile bootstrap did not create a session")
        async with _open_web_client(path, name, self._keepalive) as client:
            bound = client.auth.storage_path
            if bound is None or Path(bound).resolve() != path.resolve():
                # Fail closed: this client would read or persist another file.
                raise WebProfileUnavailable("Web profile client is bound to other storage")
            yield client

    def _session_conflict(self, name: str) -> bool:
        try:
            key = web_session_key(self._paths[name])
        except WebProfileUnavailable:
            return False
        return key is not None and bool(self._sharing_profiles(name, key))

    async def health(self, name: str) -> WebProfileHealth:
        """Report file-only diagnostics for ``name`` without opening a client."""
        path = self._paths[name]
        plan = AuthCheckPlan(
            storage_path=path,
            profile=name,
            has_env_auth=False,
            has_home_env=False,
            auth_source_label="file",
            test_fetch=False,
        )
        result = await run_auth_check(plan, read_env_auth_json=_no_env_auth_json)
        try:
            status = await asyncio.to_thread(inspect_master_token_status, path, has_env_auth=False)
            master_token_present = status.present
        except (OSError, ValueError):
            master_token_present = False
        conflict = await asyncio.to_thread(self._session_conflict, name)
        account = result.details.get("account")
        return WebProfileHealth(
            storage_exists=bool(result.checks.get("storage_exists")),
            json_valid=bool(result.checks.get("json_valid")),
            cookies_present=bool(result.checks.get("cookies_present")),
            sid_cookie=bool(result.checks.get("sid_cookie")),
            local_checks_passed=result.all_passed,
            master_token_present=master_token_present,
            session_conflict=conflict,
            account=account if isinstance(account, Mapping) else None,
        )
