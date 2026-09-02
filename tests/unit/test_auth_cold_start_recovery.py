"""Cold-start recovery regressions for issue #2068."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth import master_token as mt
from notebooklm._auth import recovery as recovery_mod
from notebooklm._auth import refresh as refresh_mod
from notebooklm._auth import session as session_mod
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.cookies import _LoadedCookiePair
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._auth.mint_service import MintService
from notebooklm._browser.headless_reauth import HeadlessReauthResult, HeadlessReauthStatus
from notebooklm._env import PERSONAL_APP_HOSTS
from notebooklm.auth import AuthTokens, fetch_tokens_with_domains
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import MissingDependencyError

_PERSONAL_HOST_PATTERN = "|".join(re.escape(host) for host in sorted(PERSONAL_APP_HOSTS))
_PERSONAL_HOMEPAGE_PATTERN = re.compile(rf"^https://(?:{_PERSONAL_HOST_PATTERN})/(?:\?.*)?$")


def _recovery_pair() -> _LoadedCookiePair:
    return _LoadedCookiePair(httpx.Cookies(), CookieJar())


def _patch_mint(effect):
    """Retarget the retired coarse mint seam to the concrete network owner."""

    async def mint(_service, token):
        return await effect(token.email, token.secret, token.android_id)

    return patch.object(MintService, "mint", autospec=True, side_effect=mint)


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

    with _patch_mint(AsyncMock(return_value=fresh_jar)) as mint:
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

    with _patch_mint(AsyncMock(return_value=fresh_jar)):
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

    import notebooklm._browser.headless_reauth as headless

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

    import notebooklm._browser.headless_reauth as headless

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

    with _patch_mint(AsyncMock(side_effect=mint)):
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
    with _patch_mint(AsyncMock(side_effect=mint)):
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
    with _patch_mint(AsyncMock(side_effect=mint)) as mint_mock:
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
    with _patch_mint(AsyncMock(side_effect=mint)) as mint_mock:
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
async def test_missing_headless_dependency_escapes_l4_instead_of_looking_revoked(
    tmp_path, monkeypatch
) -> None:
    """A configuration fault must not collapse into the rung's ``False`` decline."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    master_secret = "MASTER-SECRET-FOR-L4-TRACEBACK"
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="agent@example.com",
        master_token=master_secret,
        android_id="abc123",
    )
    monkeypatch.setitem(sys.modules, "gpsoauth", None)

    with pytest.raises(MissingDependencyError, match=r"notebooklm-py\[headless\]") as raised:
        await recovery_mod.try_master_token_reauth(
            storage_path=storage,
            cookie_jar=httpx.Cookies(),
        )
    traceback = raised.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("notebooklm."):
            for value in traceback.tb_frame.f_locals.values():
                assert value != master_secret
                if isinstance(value, mt.MasterToken):
                    assert value.secret != master_secret
        traceback = traceback.tb_next


@pytest.mark.asyncio
async def test_cold_and_live_l4_recovery_share_one_master_token_mint(tmp_path, monkeypatch) -> None:
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
        cookie_jar=live_jar,
    )
    redirect = _LoginRedirectError("Authentication expired")
    validate = AsyncMock(return_value=None)

    monkeypatch.setattr(
        recovery_mod,
        "_try_headless_reauth_result",
        AsyncMock(return_value=None),
    )
    async with NotebookLMClient(live_auth) as client:
        collaborators = client._collaborators
        lifecycle = collaborators.lifecycle
        generation = collaborators.call_supervisor._current
        expected_epoch = lifecycle._epoch
        assert lifecycle.is_open()
        assert expected_epoch > 0
        assert generation is not None and generation.epoch == expected_epoch
        assert collaborators.kernel._active_epoch == expected_epoch

        with _patch_mint(AsyncMock(side_effect=mint)) as mint_mock:
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
                session_mod._try_master_token_reauth(
                    auth=live_auth,
                    kernel=collaborators.kernel,
                    expected_epoch=expected_epoch,
                )
            )
            await asyncio.sleep(0)
            assert not live.done()
            release.set()
            cold_result, live_result = await asyncio.gather(cold, live)

        mint_mock.assert_awaited_once()
        assert live_result is True
        assert cold_result.cookie_jar.get("SID", domain=".google.com") == "fresh"
        assert (
            collaborators.kernel.get_http_client(expected_epoch=expected_epoch).cookies.get(
                "SID", domain=".google.com"
            )
            == "fresh"
        )


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

    import notebooklm._browser.headless_reauth as headless

    monkeypatch.setattr(headless, "attempt_headless_reauth", drive_browser)
    _stub_dead_then_fresh(
        httpx_mock,
        fresh_sid="master-fresh",
        csrf="csrf-master",
        session="session-master",
    )
    with _patch_mint(AsyncMock(side_effect=mint)):
        tokens = await AuthTokens.from_storage(storage, allow_headless=True)

    assert drives == 1
    assert mints == 1
    assert tokens.flat_cookies["SID"] == "master-fresh"


@pytest.mark.asyncio
async def test_cold_ladder_runs_refresh_cmd_before_the_remint_rungs(tmp_path, monkeypatch) -> None:
    """Cold start walks ADR-0030's documented order: L2.5 → L3 → L4.

    Behavior change (ADR-0030, amended 2026-08-07): this pinned L3 → L4 → L2.5
    before the alignment. Every rung's revalidation redirects except the last, so
    the whole ladder is walked and the recorded ``order`` is the rung sequence.
    It also pins that an L2.5 failure falls through instead of ending the ladder.
    It does NOT cross either former backstop rebind site — verified by mutation:
    a site-B-only re-entry survives this test. Those are covered separately by
    :func:`test_exhausted_cold_ladder_runs_the_refresh_cmd_exactly_once` (site A,
    the ladder raising) and
    :func:`test_recovered_but_still_redirecting_does_not_rerun_the_refresh_cmd`
    (site B, a re-mint whose revalidation still redirects).
    """
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    jar = httpx.Cookies()
    order: list[str] = []
    fetch_calls = 0

    async def fetch(*_args, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        order.append("fetch")
        if fetch_calls <= 3:
            raise _LoginRedirectError(f"Authentication expired or invalid. attempt={fetch_calls}")
        return "csrf", "session"

    async def refresh_cmd(*_args, **_kwargs):
        order.append("L2.5")

    async def headless(**_kwargs):
        order.append("L3")
        return _recovery_pair()

    async def master(**_kwargs):
        order.append("L4")
        return _recovery_pair()

    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", refresh_cmd)
    monkeypatch.setattr(recovery_mod, "_try_headless_reauth_result", headless)
    monkeypatch.setattr(recovery_mod, "_try_master_token_reauth_result", master)

    csrf, session, refreshed, _snapshot = await refresh_mod._fetch_tokens_with_refresh(
        jar,
        storage,
        allow_headless=True,
    )

    assert (csrf, session, refreshed) == ("csrf", "session", True)
    # fetch, L2.5, retry-fetch, L3, validate-fetch, L4, validate-fetch, retry-fetch
    assert order == ["fetch", "L2.5", "fetch", "L3", "fetch", "L4", "fetch", "fetch"]
    assert order.count("L2.5") == 1, "the rung must not also re-run as a post-ladder backstop"


@pytest.mark.asyncio
async def test_failing_refresh_cmd_still_reaches_the_remint_rungs(tmp_path, monkeypatch) -> None:
    """A broken ``NOTEBOOKLM_REFRESH_CMD`` must not MASK L3/L4 now that it runs first.

    The cold arm used to be terminal, which was safe only while it ran LAST. With
    the rung first, a non-zero exit is logged and yields "rung failed" so the
    re-mint rungs an operator recovers by today still run.
    """
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    jar = httpx.Cookies()
    fetch = AsyncMock(
        side_effect=[
            _LoginRedirectError("Authentication expired or invalid."),
            ("csrf", "session"),
            ("csrf", "session"),
        ]
    )
    broken_refresh_cmd = AsyncMock(side_effect=RuntimeError("NOTEBOOKLM_REFRESH_CMD exited 2"))
    headless = AsyncMock(return_value=None)
    master = AsyncMock(return_value=_recovery_pair())
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", broken_refresh_cmd)
    monkeypatch.setattr(recovery_mod, "_try_headless_reauth_result", headless)
    monkeypatch.setattr(recovery_mod, "_try_master_token_reauth_result", master)

    csrf, session, refreshed, _snapshot = await refresh_mod._fetch_tokens_with_refresh(
        jar,
        storage,
        allow_headless=True,
    )

    assert (csrf, session, refreshed) == ("csrf", "session", True)
    broken_refresh_cmd.assert_awaited_once()
    headless.assert_awaited_once()
    master.assert_awaited_once()


@pytest.mark.asyncio
async def test_exhausted_cold_ladder_runs_the_refresh_cmd_exactly_once(
    tmp_path, monkeypatch
) -> None:
    """The ladder-exhausted path must not re-enter L2.5 (that would be subprocess #2).

    ``_REFRESH_ATTEMPTED_CONTEXT`` is reset in the rung's ``finally`` and the
    per-path success epoch does not deduplicate a caller's own re-entry, so a
    retained post-ladder invocation would spawn a second subprocess here. When
    nothing recovers, the refresh-cmd failure is what surfaces — unchanged from
    the pre-alignment order, where the rung ran last and raised.
    """
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    jar = httpx.Cookies()
    fetch = AsyncMock(side_effect=_LoginRedirectError("Authentication expired or invalid."))
    broken_refresh_cmd = AsyncMock(side_effect=RuntimeError("refresh-cmd boom"))
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", broken_refresh_cmd)
    monkeypatch.setattr(recovery_mod, "_try_headless_reauth_result", AsyncMock(return_value=None))
    monkeypatch.setattr(
        recovery_mod, "_try_master_token_reauth_result", AsyncMock(return_value=None)
    )

    with pytest.raises(RuntimeError, match="refresh-cmd boom"):
        await refresh_mod._fetch_tokens_with_refresh(jar, storage, allow_headless=True)

    assert broken_refresh_cmd.await_count == 1


@pytest.mark.asyncio
async def test_recovered_but_still_redirecting_does_not_rerun_the_refresh_cmd(
    tmp_path, monkeypatch
) -> None:
    """Site B of the retired backstop: a re-mint whose revalidation still redirects.

    Before the alignment the refresh-cmd was the post-ladder backstop, reachable
    from TWO rebind sites: (A) the ladder itself raising, and (B) a rung that
    SUCCEEDS while the post-recovery fetch still redirects, which rebinds ``err``
    and falls through. Running the rung first consumes that role, and re-entering
    afterwards would spawn a second subprocess — the context var is reset in the
    rung's ``finally`` and the per-path success epoch does not deduplicate a
    caller's own re-entry.

    Site A is covered by the exhausted-ladder test above. Site B was covered by
    nothing: verified by mutation, a site-B-only post-ladder re-entry left the
    whole unit+integration suite green. This pins it.
    """
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    jar = httpx.Cookies()
    fetch = AsyncMock(
        side_effect=[
            _LoginRedirectError("Authentication expired or invalid. initial"),
            _LoginRedirectError("Authentication expired or invalid. l2.5-retry"),
            ("csrf", "session"),
            _LoginRedirectError("Authentication expired or invalid. outer-retry"),
        ]
    )
    refresh_cmd = AsyncMock(return_value=None)
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh-auth")
    monkeypatch.setattr(refresh_mod, "_fetch_tokens_with_jar", fetch)
    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", refresh_cmd)
    monkeypatch.setattr(
        recovery_mod,
        "_try_headless_reauth_result",
        AsyncMock(return_value=_recovery_pair()),
    )
    monkeypatch.setattr(
        recovery_mod, "_try_master_token_reauth_result", AsyncMock(return_value=None)
    )

    with pytest.raises(_LoginRedirectError):
        await refresh_mod._fetch_tokens_with_refresh(jar, storage, allow_headless=True)

    assert refresh_cmd.await_count == 1, (
        "the retired post-ladder backstop must not re-enter L2.5 when a rung "
        "recovered but its revalidation still redirects (site B)"
    )


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

    with _patch_mint(AsyncMock(side_effect=mint)):
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

    import notebooklm._browser.headless_reauth as headless

    monkeypatch.setattr(headless, "attempt_headless_reauth", drive_browser)
    _stub_dead_then_fresh(httpx_mock, fresh_sid="fresh", csrf="csrf", session="session")
    with _patch_mint(AsyncMock(side_effect=mint)):
        default_call = asyncio.create_task(AuthTokens.from_storage(storage))
        await started.wait()
        stronger_call = asyncio.create_task(AuthTokens.from_storage(storage, allow_headless=True))
        release.set()
        await asyncio.gather(default_call, stronger_call)

    assert mints == 1
    assert drives == 0


@pytest.mark.asyncio
async def test_stronger_permission_retries_after_weaker_recovery_fails(
    tmp_path, monkeypatch
) -> None:
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
            return None
        return _recovery_pair()

    validate = AsyncMock(return_value=None)
    master = AsyncMock(return_value=None)
    monkeypatch.setattr(
        recovery_mod,
        "_try_headless_reauth_result",
        AsyncMock(side_effect=try_headless),
    )
    monkeypatch.setattr(recovery_mod, "_try_master_token_reauth_result", master)
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
async def test_shared_adapter_exception_fans_out_and_later_call_retries(
    tmp_path, monkeypatch
) -> None:
    """A failed shared task records no generation or permanent in-flight slot."""
    storage = tmp_path / "storage_state.json"
    _write_storage(storage, sid="stale")
    redirect = _LoginRedirectError("Authentication expired")
    validate = AsyncMock()

    headless = AsyncMock(side_effect=RuntimeError("browser adapter failed"))
    master = AsyncMock()
    monkeypatch.setattr(recovery_mod, "_try_headless_reauth_result", headless)
    monkeypatch.setattr(recovery_mod, "_try_master_token_reauth_result", master)
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
    headless.return_value = None
    master.return_value = None
    with pytest.raises(_LoginRedirectError):
        await recovery_mod.coalesced_cold_recovery(
            storage_path=storage,
            allow_headless=True,
            validate=validate,
            initial_error=redirect,
        )

    assert headless.await_count == 2
    master.assert_awaited_once()

    typed_failure = _LoginRedirectError("typed collaborator failure")
    headless.side_effect = typed_failure
    with pytest.raises(_LoginRedirectError) as coalesced_raised:
        await recovery_mod.coalesced_cold_recovery(
            storage_path=storage,
            allow_headless=True,
            validate=validate,
            initial_error=redirect,
        )
    assert coalesced_raised.value is typed_failure
    coalesced_frames = []
    traceback = typed_failure.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__") == "notebooklm._auth.recovery":
            coalesced_frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert coalesced_frames == [
        "coalesced_cold_recovery",
        "_coalesce_cold",
        "_drive_cold",
        "run_headless",
    ]

    direct_failure = _LoginRedirectError("direct collaborator failure")
    headless.side_effect = direct_failure
    with pytest.raises(_LoginRedirectError) as direct_raised:
        await recovery_mod._run_cold_recovery(
            storage_path=storage,
            allow_headless=True,
            validate=validate,
            initial_error=redirect,
        )
    assert direct_raised.value is direct_failure
    direct_frames = []
    traceback = direct_failure.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__") == "notebooklm._auth.recovery":
            direct_frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert direct_frames == ["_run_cold_recovery", "_drive_cold", "run_headless"]
