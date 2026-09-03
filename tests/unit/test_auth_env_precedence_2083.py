"""Env-auth precedence contracts for issue #2083.

``NOTEBOOKLM_AUTH_JSON`` supplies auth inline, with no writable backing file.
``cli.services.auth_source.AuthSource`` documents the precedence — explicit
``--storage`` > env var > profile file — but three library call sites used to
spell the predicate themselves and two ranked the profile *above* the env var.
The observable consequence was a client assembled from two accounts: the CLI's
``get_client`` resolved the path through ``AuthSource`` (``None`` for env auth)
and passed the profile alongside, so the token fetch re-resolved to the profile
file while the cookie jar came from the env var.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth import cookies, refresh, tokens
from notebooklm._env import get_base_url
from notebooklm.cli.services.auth_source import AuthSource

_ENV = "NOTEBOOKLM_AUTH_JSON"


def _state(sid: str, *, authuser: int | None = None, email: str | None = None) -> dict:
    expires = time.time() + 86400
    rows = [
        ("SID", sid),
        ("APISID", "apisid"),
        ("SAPISID", "sapisid"),
        ("LSID", "lsid"),
        ("__Secure-1PSIDTS", "psidts"),
    ]
    state = {
        "cookies": [
            {"name": n, "value": v, "domain": ".google.com", "path": "/", "expires": expires}
            for n, v in rows
        ],
        "origins": [],
    }
    if authuser is not None or email is not None:
        state["notebooklm"] = {
            "version": 1,
            "account": {"authuser": authuser, "email": email},
        }
    return state


@pytest.fixture
def profiled_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``work`` profile on disk plus inline env auth for a *different* account."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    from notebooklm.paths import get_storage_path

    work = get_storage_path(profile="work")
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_text(
        json.dumps(
            _state(
                "sid-from-profile-file",
                authuser=7,
                email="profile@example.com",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV, json.dumps(_state("sid-from-env-var")))
    return work


def test_resolver_ranks_explicit_path_then_env_then_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single resolver implements exactly what ``AuthSource`` documents."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    explicit = tmp_path / "explicit.json"

    monkeypatch.setenv(_ENV, json.dumps(_state("x")))
    assert cookies.resolve_auth_storage_path(explicit, "work") == explicit
    assert cookies.resolve_auth_storage_path(None, "work") is None
    assert cookies.resolve_auth_storage_path(None, None) is None

    monkeypatch.delenv(_ENV)
    assert cookies.resolve_auth_storage_path(None, "work") is not None


def test_resolver_treats_an_empty_env_value_as_env_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence, not truthiness — matching ``_load_storage_state``.

    An empty value is a configuration error the loader reports by name. Falling
    through to a profile file would hide it and silently authenticate as a
    different account.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.setenv(_ENV, "")

    assert cookies.resolve_auth_storage_path(None, "work") is None
    with pytest.raises(ValueError, match="set but empty"):
        cookies._load_storage_state(None)


def test_library_and_authsource_agree_on_env_over_profile(profiled_home: Path) -> None:
    """The divergence that produced a two-account client (#2083)."""
    source = AuthSource.from_components(storage_override=None, profile="work")
    assert source.has_env_auth is True
    assert source.resolve() is None

    assert cookies.resolve_auth_storage_path(None, "work") is None


@pytest.mark.asyncio
async def test_token_fetch_and_cookie_jar_read_the_same_account(
    profiled_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    """``get_client``'s two halves must not disagree.

    It calls ``fetch_tokens_with_domains(resolved_path, profile)`` and then
    ``load_auth_from_storage(resolved_path)``. With env auth the resolved path
    is ``None``; before the fix the profile argument made only the first half
    re-resolve to the profile file.
    """
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    httpx_mock.add_response(
        url="https://notebook.google.com/",
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
    )

    assert await refresh.fetch_tokens_with_domains(None, "work") == ("csrf", "session")

    request = httpx_mock.get_request()
    assert request is not None
    assert cookies.resolve_auth_storage_path(None, "work") is None
    profile_account = json.loads(profiled_home.read_text(encoding="utf-8"))["notebooklm"]["account"]
    assert profile_account == {"authuser": 7, "email": "profile@example.com"}
    cookie_header = request.headers.get("cookie", "")
    assert "SID=sid-from-env-var" in cookie_header
    assert "sid-from-profile-file" not in cookie_header
    assert str(request.url) == "https://notebook.google.com/"
    assert request.url.params.get("authuser") is None
    assert tokens.load_auth_from_storage(None)["SID"] == "sid-from-env-var"


@pytest.mark.asyncio
async def test_auth_tokens_from_storage_keeps_env_auth_under_a_profile(
    profiled_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AuthTokens.from_storage(profile=...)`` must not silently become file auth."""
    seen: dict[str, object] = {}

    async def stop_after_resolution(self, source, policy):
        seen["source"] = source
        raise RuntimeError("stop after resolution")

    monkeypatch.setattr(tokens.SessionSeedLoader, "load", stop_after_resolution)
    with pytest.raises(RuntimeError, match="stop after resolution"):
        await tokens.AuthTokens.from_storage(profile="work")

    assert isinstance(seen.get("source"), tokens.InlineAuthSource)


@pytest.mark.asyncio
async def test_refresh_cmd_does_not_run_or_touch_a_profile_under_env_auth(
    profiled_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    """Env auth has no file to refresh into, so the hook must not fire.

    Running it would lock, rewrite and then *read* a profile file the caller
    bypassed — converting env auth into file auth. It also cannot succeed:
    ``NOTEBOOKLM_AUTH_JSON`` is scrubbed from the subprocess environment, so
    the command cannot re-mint the credential actually in use.
    """
    marker = tmp_path / "refresh-cmd-ran"
    monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", f"touch {marker}")
    before = profiled_home.read_bytes()

    # A real sign-in redirect, not a patched failure: the homepage GET bounces
    # to the login page, extraction reports "redirected to ...", and that string
    # is one of the auth signals ``_should_try_refresh`` fires on.
    httpx_mock.add_response(
        url=f"{get_base_url()}/",
        status_code=302,
        headers={"Location": "https://accounts.google.com/signin"},
    )
    httpx_mock.add_response(
        url="https://accounts.google.com/signin",
        content=b"<html>Login</html>",
    )

    with pytest.raises(ValueError):
        await refresh.fetch_tokens_with_domains(None, "work")

    assert not marker.exists(), "the refresh command must not run for env-only auth"
    assert profiled_home.read_bytes() == before, "a bypassed profile must not be rewritten"


@pytest.mark.asyncio
async def test_explicit_cookies_still_refresh_when_env_var_is_merely_present(
    profiled_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    """``fetch_tokens(cookies=...)`` did not load from the env var, so the hook applies.

    The env-auth suppression must key off how *this* caller obtained its
    credentials, not off ``storage_path is None`` plus an ambient
    ``NOTEBOOKLM_AUTH_JSON`` that may belong to something else in the process.
    Inferring it would silently disable ``NOTEBOOKLM_REFRESH_CMD`` for every
    explicit-cookie caller in a mixed environment (#2084 review).
    """
    # The whole point is a *mixed* environment, and that precondition arrives
    # via the fixture rather than this body — state it so it cannot erode
    # silently and leave the test passing for the wrong reason.
    assert _ENV in os.environ, "this test is only meaningful with ambient env auth set"
    marker = tmp_path / "refresh-cmd-ran"
    monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", f"touch {marker}")
    # Two rounds: the initial fetch, then the post-refresh retry.
    for _ in range(2):
        httpx_mock.add_response(
            url=re.compile(rf"^{re.escape(get_base_url())}/(?:\?.*)?$"),
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin", content=b"<html>Login</html>"
        )

    with pytest.raises(Exception):  # noqa: B017 - the refresh attempt is the subject
        await refresh.fetch_tokens({"SID": "sid", "__Secure-1PSIDTS": "psidts"}, profile="work")

    assert marker.exists(), "explicit-cookie callers must keep their refresh hook"
