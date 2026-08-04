"""Cold-start recovery regressions for issue #2068."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth import master_token as mt
from notebooklm._auth import recovery as recovery_mod
from notebooklm._auth import refresh as refresh_mod
from notebooklm._auth import session as session_mod
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._auth.headless_reauth import HeadlessReauthResult, HeadlessReauthStatus
from notebooklm._env import PERSONAL_APP_HOSTS
from notebooklm.auth import AuthTokens, fetch_tokens_with_domains
from notebooklm.client import NotebookLMClient

_PERSONAL_HOST_PATTERN = "|".join(re.escape(host) for host in sorted(PERSONAL_APP_HOSTS))
_PERSONAL_HOMEPAGE_PATTERN = re.compile(rf"^https://(?:{_PERSONAL_HOST_PATTERN})/(?:\?.*)?$")


def _write_storage(path, *, sid: str) -> None:
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": sid, "domain": ".google.com"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": f"{sid}-ts",
                        "domain": ".google.com",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def _stub_dead_then_fresh(
    httpx_mock: HTTPXMock, *, fresh_sid: str, csrf: str, session: str
) -> None:
    def homepage(request: httpx.Request) -> httpx.Response:
        if f"SID={fresh_sid}" in request.headers.get("cookie", ""):
            return httpx.Response(
                200,
                content=f'"SNlM0e":"{csrf}" "FdrFJe":"{session}"'.encode(),
                headers={"Set-Cookie": "RECOVERY_DELTA=kept; Domain=.google.com; Path=/"},
                request=request,
            )
        return httpx.Response(
            302,
            headers={"Location": "https://accounts.google.com/signin"},
            request=request,
        )

    httpx_mock.add_callback(
        homepage,
        url=_PERSONAL_HOMEPAGE_PATTERN,
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://accounts.google.com/signin",
        content=b"<html>Login</html>",
        is_reusable=True,
    )


@pytest.mark.asyncio
async def test_auth_tokens_cold_start_remints_from_sibling_master_token(
    tmp_path, httpx_mock: HTTPXMock
) -> None:
    """A dead file-backed session can recover before a client exists."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )

    fresh_jar = httpx.Cookies()
    fresh_jar.set("SID", "fresh", domain=".google.com")
    fresh_jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")

    _stub_dead_then_fresh(
        httpx_mock,
        fresh_sid="fresh",
        csrf="csrf-fresh",
        session="session-fresh",
    )

    with patch.object(mt, "mint_cookies", new=AsyncMock(return_value=fresh_jar)) as mint:
        tokens = await AuthTokens.from_storage(storage)

    mint.assert_awaited_once()
    assert tokens.csrf_token == "csrf-fresh"
    assert tokens.session_id == "session-fresh"
    assert tokens.flat_cookies["SID"] == "fresh"
    assert tokens.flat_cookies["RECOVERY_DELTA"] == "kept"
    assert tokens.account_email == "agent@example.com"
    stored_names = {
        cookie["name"] for cookie in json.loads(storage.read_text(encoding="utf-8"))["cookies"]
    }
    assert "RECOVERY_DELTA" in stored_names


@pytest.mark.asyncio
async def test_client_factory_reaches_cold_master_token_recovery(
    tmp_path, httpx_mock: HTTPXMock
) -> None:
    """The public lazy client factory reaches the same pre-client recovery seam."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    fresh_jar = httpx.Cookies()
    fresh_jar.set("SID", "fresh", domain=".google.com")
    fresh_jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
    _stub_dead_then_fresh(httpx_mock, fresh_sid="fresh", csrf="csrf", session="session")

    with patch.object(mt, "mint_cookies", new=AsyncMock(return_value=fresh_jar)):
        client = await NotebookLMClient.from_storage(path=str(storage))._build()

    assert client.auth.csrf_token == "csrf"
    assert client.auth.flat_cookies["SID"] == "fresh"


@pytest.mark.asyncio
async def test_auth_tokens_cold_start_headless_recovery_is_explicit(
    tmp_path, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """The Python cold factory exposes one-call L3 permission."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")

    def drive_browser(**kwargs):
        assert kwargs["storage_path"] == storage
        assert kwargs["allow_headless"] is True
        _write_storage(storage, sid="browser-fresh")
        return HeadlessReauthResult(HeadlessReauthStatus.SUCCESS, "ok", storage_path=storage)

    import notebooklm._auth.headless_reauth as headless

    monkeypatch.setattr(headless, "attempt_headless_reauth", drive_browser)
    _stub_dead_then_fresh(
        httpx_mock,
        fresh_sid="browser-fresh",
        csrf="csrf-browser",
        session="session-browser",
    )

    tokens = await AuthTokens.from_storage(storage, allow_headless=True)

    assert tokens.flat_cookies["SID"] == "browser-fresh"
    assert tokens.csrf_token == "csrf-browser"


@pytest.mark.asyncio
async def test_auth_tokens_cold_start_headless_recovery_honors_env(
    tmp_path, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """The environment opt-in reaches L3 without a per-call flag."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    browser_profile = tmp_path / "browser_profile"
    browser_profile.mkdir()
    (browser_profile / "Preferences").write_text("{}", encoding="utf-8")
    drives = 0

    def drive_browser(plan, io, *, headless, interactive):
        nonlocal drives
        drives += 1
        assert plan.storage_path == storage
        assert plan.browser_profile == browser_profile
        assert headless is True
        assert interactive is False
        _write_storage(storage, sid="browser-fresh")

    import notebooklm._auth.headless_reauth as headless

    monkeypatch.setenv("NOTEBOOKLM_HEADLESS_REAUTH", "1")
    monkeypatch.setattr(headless, "_playwright_installed", lambda: True)
    monkeypatch.setattr(headless, "run_browser_capture", drive_browser)
    _stub_dead_then_fresh(
        httpx_mock,
        fresh_sid="browser-fresh",
        csrf="csrf-browser",
        session="session-browser",
    )

    tokens = await AuthTokens.from_storage(storage)

    assert drives == 1
    assert tokens.flat_cookies["SID"] == "browser-fresh"
    assert tokens.csrf_token == "csrf-browser"


@pytest.mark.asyncio
async def test_concurrent_cold_start_coalesces_one_master_token_mint(
    tmp_path, httpx_mock: HTTPXMock
) -> None:
    """Equivalent overlapping constructors share one L4 recovery."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    mint_count = 0

    async def mint(*args):
        nonlocal mint_count
        mint_count += 1
        await asyncio.sleep(0.02)
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    def homepage(request: httpx.Request) -> httpx.Response:
        if "SID=fresh" in request.headers.get("cookie", ""):
            return httpx.Response(
                200,
                content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
                request=request,
            )
        return httpx.Response(
            302,
            headers={"Location": "https://accounts.google.com/signin"},
            request=request,
        )

    httpx_mock.add_callback(
        homepage,
        url=_PERSONAL_HOMEPAGE_PATTERN,
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://accounts.google.com/signin",
        content=b"<html>Login</html>",
        is_reusable=True,
    )

    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)):
        first, second = await asyncio.gather(
            AuthTokens.from_storage(storage),
            AuthTokens.from_storage(storage),
        )

    assert mint_count == 1
    assert first.flat_cookies["SID"] == second.flat_cookies["SID"] == "fresh"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_master_token_mint(
    tmp_path, httpx_mock: HTTPXMock
) -> None:
    """A cancelled caller waits for settlement while a follower succeeds."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    mint_count = 0

    async def mint(*args):
        nonlocal mint_count
        mint_count += 1
        started.set()
        await release.wait()
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    _stub_dead_then_fresh(httpx_mock, fresh_sid="fresh", csrf="csrf", session="session")
    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)):
        cancelled = asyncio.create_task(AuthTokens.from_storage(storage))
        await started.wait()
        follower = asyncio.create_task(AuthTokens.from_storage(storage))
        await asyncio.sleep(0)
        cancelled.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        tokens = await follower

    assert mint_count == 1
    assert tokens.flat_cookies["SID"] == "fresh"


@pytest.mark.asyncio
async def test_cancelled_direct_l4_waiter_does_not_cancel_shared_mint(tmp_path) -> None:
    """Cancellation of one L4 waiter leaves the shared mint available to a follower."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def mint(*args):
        started.set()
        await release.wait()
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    cancelled_jar = httpx.Cookies()
    follower_jar = httpx.Cookies()
    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)) as mint_mock:
        cancelled = asyncio.create_task(
            recovery_mod.try_master_token_reauth(
                storage_path=storage,
                cookie_jar=cancelled_jar,
            )
        )
        await started.wait()
        follower = asyncio.create_task(
            recovery_mod.try_master_token_reauth(
                storage_path=storage,
                cookie_jar=follower_jar,
            )
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert await follower is True

    mint_mock.assert_awaited_once()
    assert "SID" not in cancelled_jar
    assert follower_jar.get("SID", domain=".google.com") == "fresh"


@pytest.mark.asyncio
async def test_shared_l4_failure_fans_out_and_later_call_retries(tmp_path) -> None:
    """A failed shared L4 task is removed so a later caller can mint again."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def mint(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await release.wait()
            raise mt.MasterTokenError("revoked once")
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    first_jar = httpx.Cookies()
    second_jar = httpx.Cookies()
    retry_jar = httpx.Cookies()
    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)) as mint_mock:
        first = asyncio.create_task(
            recovery_mod.try_master_token_reauth(storage_path=storage, cookie_jar=first_jar)
        )
        await started.wait()
        second = asyncio.create_task(
            recovery_mod.try_master_token_reauth(storage_path=storage, cookie_jar=second_jar)
        )
        await asyncio.sleep(0)
        assert not second.done()
        release.set()

        assert await asyncio.gather(first, second) == [False, False]
        assert (
            await recovery_mod.try_master_token_reauth(
                storage_path=storage,
                cookie_jar=retry_jar,
            )
            is True
        )

    assert mint_mock.await_count == 2
    assert "SID" not in first_jar
    assert "SID" not in second_jar
    assert retry_jar.get("SID", domain=".google.com") == "fresh"


@pytest.mark.asyncio
async def test_cold_and_live_l4_recovery_share_one_master_token_mint(tmp_path) -> None:
    """Overlapping cold and live recovery for one path join the same L4 task."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def mint(*args):
        started.set()
        await release.wait()
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    live_jar = httpx.Cookies()
    live_auth = AuthTokens(
        cookies={},
        csrf_token="stale-csrf",
        session_id="stale-session",
        storage_path=storage,
    )
    kernel = MagicMock()
    kernel.get_http_client.return_value.cookies = live_jar
    redirect = _LoginRedirectError("Authentication expired")
    validate = AsyncMock(return_value=None)

    with (
        patch.object(recovery_mod, "try_headless_reauth", new=AsyncMock(return_value=False)),
        patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)) as mint_mock,
    ):
        cold = asyncio.create_task(
            recovery_mod.coalesced_cold_recovery(
                storage_path=storage,
                allow_headless=False,
                validate=validate,
                initial_error=redirect,
            )
        )
        await started.wait()
        live = asyncio.create_task(
            session_mod._try_master_token_reauth(auth=live_auth, kernel=kernel)
        )
        await asyncio.sleep(0)
        assert not live.done()
        release.set()
        cold_result, live_result = await asyncio.gather(cold, live)

    mint_mock.assert_awaited_once()
    assert live_result is True
    assert cold_result.cookie_jar.get("SID", domain=".google.com") == "fresh"
    assert live_jar.get("SID", domain=".google.com") == "fresh"


@pytest.mark.asyncio
async def test_headless_retry_that_still_redirects_falls_through_to_l4(
    tmp_path, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """Each cold recovery layer runs once, in L3 then L4 order."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    drives = 0
    mints = 0

    def drive_browser(**kwargs):
        nonlocal drives
        drives += 1
        _write_storage(storage, sid="browser-still-dead")
        return HeadlessReauthResult(HeadlessReauthStatus.SUCCESS, "captured", storage_path=storage)

    async def mint(*args):
        nonlocal mints
        mints += 1
        jar = httpx.Cookies()
        jar.set("SID", "master-fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "master-fresh-ts", domain=".google.com")
        return jar

    import notebooklm._auth.headless_reauth as headless

    monkeypatch.setattr(headless, "attempt_headless_reauth", drive_browser)
    _stub_dead_then_fresh(
        httpx_mock,
        fresh_sid="master-fresh",
        csrf="csrf-master",
        session="session-master",
    )
    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)):
        tokens = await AuthTokens.from_storage(storage, allow_headless=True)

    assert drives == 1
    assert mints == 1
    assert tokens.flat_cookies["SID"] == "master-fresh"


@pytest.mark.asyncio
async def test_both_cold_validations_redirect_before_l5(tmp_path, monkeypatch) -> None:
    """L5 sees the final typed redirect after both cold layers fail validation."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    jar = httpx.Cookies()
    redirects = [
        _LoginRedirectError(f"Authentication expired or invalid. attempt={index}")
        for index in range(3)
    ]
    fetch = AsyncMock(side_effect=[*redirects, ("csrf", "session")])
    headless = AsyncMock(return_value=True)
    master = AsyncMock(return_value=True)
    run_l5 = AsyncMock(return_value=None)
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", run_l5)
    monkeypatch.setattr(recovery_mod, "try_headless_reauth", headless)
    monkeypatch.setattr(recovery_mod, "try_master_token_reauth", master)

    csrf, session, refreshed, _snapshot = await refresh_mod._fetch_tokens_with_refresh(
        jar,
        storage,
        allow_headless=True,
    )

    assert (csrf, session, refreshed) == ("csrf", "session", True)
    headless.assert_awaited_once()
    master.assert_awaited_once()
    run_l5.assert_awaited_once()
    assert fetch.await_count == 4


@pytest.mark.asyncio
async def test_same_path_callers_keep_their_explicit_account_routes(
    tmp_path, httpx_mock: HTTPXMock
) -> None:
    """Shared recovery never shares route-specific token results."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="stored@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    mint_count = 0

    async def mint(*args):
        nonlocal mint_count
        mint_count += 1
        await asyncio.sleep(0.01)
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    def homepage(request: httpx.Request) -> httpx.Response:
        if "SID=fresh" not in request.headers.get("cookie", ""):
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin"},
                request=request,
            )
        route = request.url.params.get("authuser", "default")
        return httpx.Response(
            200,
            content=f'"SNlM0e":"csrf-{route}" "FdrFJe":"session-{route}"'.encode(),
            request=request,
        )

    httpx_mock.add_callback(
        homepage,
        url=_PERSONAL_HOMEPAGE_PATTERN,
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://accounts.google.com/signin",
        content=b"<html>Login</html>",
        is_reusable=True,
    )

    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)):
        by_index, by_email = await asyncio.gather(
            fetch_tokens_with_domains(storage, authuser=1),
            fetch_tokens_with_domains(storage, account_email="other@example.com"),
        )

    assert mint_count == 1
    assert by_index == ("csrf-1", "session-1")
    assert by_email == ("csrf-other@example.com", "session-other@example.com")


@pytest.mark.asyncio
async def test_mixed_headless_permissions_serialize_and_reuse_l4_success(
    tmp_path, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    """A stronger heterogeneous waiter reuses an earlier automatic L4 heal."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token="aas_et/test",
        android_id="abc123",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    drives = 0
    mints = 0

    def drive_browser(**kwargs):
        nonlocal drives
        if kwargs["allow_headless"]:
            drives += 1
        return HeadlessReauthResult(HeadlessReauthStatus.UNAVAILABLE, "unused")

    async def mint(*args):
        nonlocal mints
        mints += 1
        started.set()
        await release.wait()
        jar = httpx.Cookies()
        jar.set("SID", "fresh", domain=".google.com")
        jar.set("__Secure-1PSIDTS", "fresh-ts", domain=".google.com")
        return jar

    import notebooklm._auth.headless_reauth as headless

    monkeypatch.setattr(headless, "attempt_headless_reauth", drive_browser)
    _stub_dead_then_fresh(httpx_mock, fresh_sid="fresh", csrf="csrf", session="session")
    with patch.object(mt, "mint_cookies", new=AsyncMock(side_effect=mint)):
        default_call = asyncio.create_task(AuthTokens.from_storage(storage))
        await started.wait()
        stronger_call = asyncio.create_task(AuthTokens.from_storage(storage, allow_headless=True))
        release.set()
        await asyncio.gather(default_call, stronger_call)

    assert mints == 1
    assert drives == 0


@pytest.mark.asyncio
async def test_stronger_permission_retries_after_weaker_recovery_fails(tmp_path) -> None:
    """A failed weak ladder does not suppress a queued stronger L3 attempt."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    redirect = _LoginRedirectError("Authentication expired")
    started = asyncio.Event()
    release = asyncio.Event()
    permissions: list[bool] = []

    async def try_headless(*, allow_headless, **kwargs):
        permissions.append(allow_headless)
        if not allow_headless:
            started.set()
            await release.wait()
            return False
        return True

    validate = AsyncMock(return_value=None)
    with (
        patch.object(recovery_mod, "try_headless_reauth", side_effect=try_headless),
        patch.object(
            recovery_mod,
            "try_master_token_reauth",
            new=AsyncMock(return_value=False),
        ) as master,
    ):
        weak = asyncio.create_task(
            recovery_mod.coalesced_cold_recovery(
                storage_path=storage,
                allow_headless=False,
                validate=validate,
                initial_error=redirect,
            )
        )
        await started.wait()
        strong = asyncio.create_task(
            recovery_mod.coalesced_cold_recovery(
                storage_path=storage,
                allow_headless=True,
                validate=validate,
                initial_error=redirect,
            )
        )
        await asyncio.sleep(0)
        release.set()
        weak_result, strong_result = await asyncio.gather(
            weak,
            strong,
            return_exceptions=True,
        )

    assert isinstance(weak_result, _LoginRedirectError)
    assert isinstance(strong_result, recovery_mod.ColdRecoveryResult)
    assert permissions == [False, True]
    master.assert_awaited_once()
    validate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "NotebookLM redirected to a region / anti-abuse access gate.",
        "Google reported CookieMismatch while loading NotebookLM.",
        "CSRF token not found in HTML.",
    ],
)
async def test_non_login_failures_never_enter_cold_recovery(tmp_path, message, monkeypatch) -> None:
    """Only the private typed login redirect can trigger L3/L4."""
    jar = httpx.Cookies()
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")

    fetch = AsyncMock(side_effect=ValueError(message))
    recover = AsyncMock()
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(recovery_mod, "coalesced_cold_recovery", recover)

    with pytest.raises(ValueError, match=re.escape(message)):
        await refresh_mod._fetch_tokens_with_refresh(jar, storage)

    recover.assert_not_awaited()


@pytest.mark.asyncio
async def test_cookie_mismatch_skips_l3_l4_but_preserves_legacy_l5(tmp_path, monkeypatch) -> None:
    """The new typed gate must not narrow the existing refresh-command matcher."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    jar = httpx.Cookies()
    mismatch = ValueError(
        "Google reported CookieMismatch. Run 'notebooklm login' to re-authenticate."
    )
    fetch = AsyncMock(side_effect=[mismatch, ("csrf", "session")])
    run_l5 = AsyncMock(return_value=None)
    recover = AsyncMock()
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", run_l5)
    monkeypatch.setattr(recovery_mod, "coalesced_cold_recovery", recover)

    csrf, session, refreshed, _snapshot = await refresh_mod._fetch_tokens_with_refresh(jar, storage)

    assert (csrf, session, refreshed) == ("csrf", "session", True)
    recover.assert_not_awaited()
    run_l5.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_adapter_exception_fans_out_and_later_call_retries(tmp_path) -> None:
    """A failed shared task records no generation or permanent in-flight slot."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    redirect = _LoginRedirectError("Authentication expired")
    validate = AsyncMock()

    with (
        patch.object(
            recovery_mod,
            "try_headless_reauth",
            new=AsyncMock(side_effect=RuntimeError("browser adapter failed")),
        ) as headless,
        patch.object(recovery_mod, "try_master_token_reauth", new_callable=AsyncMock) as master,
    ):
        first, second = await asyncio.gather(
            recovery_mod.coalesced_cold_recovery(
                storage_path=storage,
                allow_headless=True,
                validate=validate,
                initial_error=redirect,
            ),
            recovery_mod.coalesced_cold_recovery(
                storage_path=storage,
                allow_headless=True,
                validate=validate,
                initial_error=redirect,
            ),
            return_exceptions=True,
        )
        assert isinstance(first, RuntimeError)
        assert isinstance(second, RuntimeError)
        assert headless.await_count == 1
        master.assert_not_awaited()

        headless.side_effect = None
        headless.return_value = False
        master.return_value = False
        with pytest.raises(_LoginRedirectError):
            await recovery_mod.coalesced_cold_recovery(
                storage_path=storage,
                allow_headless=True,
                validate=validate,
                initial_error=redirect,
            )

    assert headless.await_count == 2
    master.assert_awaited_once()
