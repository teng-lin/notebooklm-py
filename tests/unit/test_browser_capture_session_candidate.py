"""Anonymous app landings must never replace a stored session (#2467).

The browser and clock are local fakes: these tests make no requests, rotate no
cookies, and use synthetic values. The exported jar deliberately differs from
the browser's candidate cookies in the final-guard cases.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import MagicMock, PropertyMock
from urllib.parse import parse_qs, urlparse

import pytest

from notebooklm._browser import browser_capture as capture
from notebooklm.exceptions import HeadlessLoginRequiredError

pytest.importorskip("playwright")

APP = "https://notebook.google.com/"
SIGN_IN = "https://accounts.google.com/v3/signin/identifier"
SID = {"name": "SID", "value": "synthetic-sid", "domain": ".google.com", "path": "/"}
NID = {"name": "NID", "value": "synthetic-nid", "domain": ".google.com", "path": "/"}


class _InteractiveExit(Exception):
    pass


class _IO:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.messages.append(str(args[0]) if args else "")

    def fail(self, code: int) -> NoReturn:
        assert code == 1
        raise _InteractiveExit

    def run_async(self, coro: Any) -> Any:
        raise AssertionError("the browser core must not dispatch async work")


class _Browser:
    def __init__(self, *, cookies: list[dict[str, Any]], url: str = APP) -> None:
        self.now = 1000.0
        self.page = MagicMock()
        self.page.url = url
        self.page.content.return_value = "<html></html>"
        self.page.goto.side_effect = self.goto
        self.page.wait_for_url.side_effect = self.wait_for_url
        self.page.wait_for_timeout.side_effect = self.wait_for_timeout
        self.context = MagicMock()
        self.context.pages = [self.page]
        self.page.context = self.context
        self.context.new_page.return_value = self.page
        self.context.cookies.side_effect = self.cookies
        self.context.storage_state.side_effect = lambda: deepcopy(self.state)
        self.state: dict[str, Any] = {"cookies": deepcopy(cookies), "origins": []}
        self.browser_cookies = deepcopy(cookies)
        self.inside_matcher = False
        self.arrive_at: float | None = None
        self.finish_sign_in = False
        self.finish_cookie_forcing = True
        self.app_visits = 0
        self.cdp = MagicMock()
        self.cdp.contexts = [self.context]
        self.playwright = MagicMock()
        self.playwright.chromium.launch_persistent_context.return_value = self.context
        self.playwright.chromium.connect_over_cdp.return_value = self.cdp

    def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        assert not self.inside_matcher, "cookie reads must not reenter the URL predicate"
        assert urls == [self.page.url], "candidate cookies must be URL-scoped"
        return deepcopy(self.browser_cookies)

    def goto(self, url: str, **kwargs: Any) -> None:
        assert kwargs["wait_until"] == "commit"
        if "ServiceLogin?" in url:
            self.page.url = SIGN_IN
        elif url == capture.GOOGLE_ACCOUNTS_URL:
            self.page.url = capture.GOOGLE_ACCOUNTS_URL
        else:
            self.app_visits += 1
            if self.app_visits == 1 or self.finish_cookie_forcing:
                self.page.url = APP
            else:
                self.page.url = SIGN_IN

    def wait_for_url(self, matcher: Any, **kwargs: Any) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        assert kwargs["wait_until"] == "commit"
        self.inside_matcher = True
        try:
            assert matcher(APP)
            assert not matcher(SIGN_IN)
        finally:
            self.inside_matcher = False
        if self.finish_sign_in and self.page.url == SIGN_IN:
            self.page.url = APP
            self.browser_cookies = [deepcopy(SID)]
            self.state["cookies"] = [deepcopy(SID)]
        if self.page.url == SIGN_IN:
            self.now += kwargs["timeout"] / 1000
            raise PlaywrightTimeout("synthetic sign-in timeout")

    def wait_for_timeout(self, milliseconds: float) -> None:
        assert 0 < milliseconds <= capture.CAPTURE_POLL_MS
        self.now += milliseconds / 1000
        if self.arrive_at is not None and self.now >= self.arrive_at:
            self.browser_cookies = [deepcopy(SID)]
            self.state["cookies"] = [deepcopy(SID)]


@pytest.fixture(autouse=True)
def _configured_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)


def _install_browser(monkeypatch: pytest.MonkeyPatch, browser: _Browser) -> MagicMock:
    from contextlib import contextmanager

    @contextmanager
    def playwright_context():
        yield browser.playwright

    # Substitute the external browser gateway; auth module patches belong to
    # the individual tests under the survivor policy.
    monkeypatch.setattr("playwright.sync_api.sync_playwright", playwright_context)
    monkeypatch.setattr(capture.time, "monotonic", lambda: browser.now)
    heal = MagicMock(side_effect=lambda state: (state, ValueError("synthetic missing PSIDTS")))
    return heal


def _run(mode: str, plan: capture.BrowserCapturePlan, io: _IO) -> Any:
    if mode == "cdp":
        return capture.run_cdp_capture(plan, io, cdp_url="http://127.0.0.1:9222")
    return capture.run_browser_capture(
        plan, io, headless=mode == "headless", interactive=mode == "interactive"
    )


def _existing_plan(tmp_path: Path, *, timeout: int = 6) -> capture.BrowserCapturePlan:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [{**SID, "value": "existing-sid"}],
                "origins": [],
                "notebooklm": {"version": 1, "account": {"authuser": 2}},
            }
        ),
        encoding="utf-8",
    )
    return capture.BrowserCapturePlan(
        browser="chromium",
        browser_profile=tmp_path / "browser",
        storage_path=storage,
        login_timeout_s=timeout,
    )


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_nid_only_app_landing_preserves_existing_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()

    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, io)

    writer.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before
    assert not any("Capturing Google cookies" in message for message in io.messages)
    assert not any("Already logged in" in message for message in io.messages)
    assert not any("Login detected" in message for message in io.messages)
    service_logins = [
        call for call in browser.page.goto.call_args_list if "ServiceLogin?" in call.args[0]
    ]
    assert len(service_logins) == (1 if mode == "interactive" else 0)
    if mode != "interactive":
        browser.page.wait_for_url.assert_not_called()


def test_interactive_anonymous_landing_gets_one_encoded_sign_in_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser(cookies=[NID])
    browser.finish_sign_in = True
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    io = _IO()

    _run("interactive", plan, io)

    service_logins = [
        call for call in browser.page.goto.call_args_list if "ServiceLogin?" in call.args[0]
    ]
    assert len(service_logins) == 1
    fallback = service_logins[0]
    assert parse_qs(urlparse(fallback.args[0]).query) == {"continue": [APP]}
    assert "%3A%2F%2F" in fallback.args[0]
    assert fallback.kwargs["timeout"] == 4000
    assert browser.page.wait_for_url.call_args.kwargs["timeout"] == 4000
    assert len(browser.page.wait_for_timeout.call_args_list) == 4
    heal.assert_called_once()
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]
    assert "account" not in json.loads(plan.storage_path.read_text())["notebooklm"]
    assert any("Capturing Google cookies..." in message for message in io.messages)


@pytest.mark.parametrize(
    "error",
    [
        "net::ERR_CONNECTION_RESET",
        "net::ERR_CONNECTION_REFUSED",
        "net::ERR_INVALID_URL",
        "Protocol error",
    ],
)
def test_owned_sign_in_continuation_does_not_hide_non_race_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])

    def fail_continuation(url: str, **kwargs: Any) -> None:
        if "ServiceLogin?" in url:
            raise PlaywrightError(error)
        browser.goto(url, **kwargs)

    browser.page.goto.side_effect = fail_continuation
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    with pytest.raises(PlaywrightError, match=error):
        _run("interactive", plan, _IO())

    browser.page.wait_for_url.assert_not_called()
    writer.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before


@pytest.mark.parametrize("failure", ["timeout", "closed"])
def test_owned_sign_in_continuation_preserves_timeout_and_closed_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    browser = _Browser(cookies=[NID])

    def fail_continuation(url: str, **kwargs: Any) -> None:
        if "ServiceLogin?" in url:
            if failure == "timeout":
                raise PlaywrightTimeout("synthetic timeout")
            raise PlaywrightError(capture.TARGET_CLOSED_ERROR)
        browser.goto(url, **kwargs)

    browser.page.goto.side_effect = fail_continuation
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    with pytest.raises(_InteractiveExit):
        _run("interactive", plan, io)

    expected = "Login not detected" if failure == "timeout" else "browser window was closed"
    assert any(expected in message for message in io.messages)
    browser.page.wait_for_url.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before


def test_superseded_sign_in_continuation_still_waits_for_sid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])
    browser.finish_sign_in = True

    def superseded_continuation(url: str, **kwargs: Any) -> None:
        browser.goto(url, **kwargs)
        if "ServiceLogin?" in url:
            raise PlaywrightError("net::ERR_ABORTED")

    browser.page.goto.side_effect = superseded_continuation
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)

    _run("interactive", plan, _IO())

    browser.page.wait_for_url.assert_called_once()
    heal.assert_called_once()
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_same_document_sid_arrival_during_settle_is_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    browser = _Browser(cookies=[NID])
    browser.arrive_at = browser.now + 1
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)

    _run(mode, plan, _IO())

    assert browser.app_visits >= 1
    assert len(browser.page.wait_for_timeout.call_args_list) == 2
    assert all("ServiceLogin?" not in call.args[0] for call in browser.page.goto.call_args_list)
    assert {row["name"] for row in json.loads(plan.storage_path.read_text())["cookies"]} == {"SID"}
    heal.assert_called_once()


def test_on_host_incomplete_wait_is_paced_and_times_out_without_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    with pytest.raises(PlaywrightTimeout):
        capture.wait_for_login_landing(browser.page, timeout_s=1.2)

    assert browser.now == pytest.approx(1001.2)
    assert [call.args[0] for call in browser.page.wait_for_timeout.call_args_list] == pytest.approx(
        [500, 500, 200]
    )
    assert len(browser.page.wait_for_url.call_args_list) == 3
    heal.assert_not_called()


def test_on_host_wait_detects_same_document_sid_arrival(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = _Browser(cookies=[NID])
    browser.arrive_at = browser.now + 1
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)

    assert capture.wait_for_login_landing(browser.page, timeout_s=3) == 0

    assert browser.now == 1001
    assert browser.page.wait_for_timeout.call_count == 2
    heal.assert_not_called()


def test_paced_no_sid_observations_reset_the_immediate_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    total_failures = capture.MAX_TOLERATED_NAVIGATION_FAILURES + 5
    attempts = 0

    def alternate_failure_and_paced_observation(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2 * total_failures:
            if attempts % 2:
                raise PlaywrightError("net::ERR_ABORTED")
        else:
            browser.browser_cookies = [deepcopy(SID)]

    browser.page.wait_for_url.side_effect = alternate_failure_and_paced_observation

    assert capture.wait_for_login_landing(browser.page, timeout_s=30) == total_failures

    assert attempts == 2 * total_failures + 1
    assert browser.page.wait_for_timeout.call_count == total_failures
    assert browser.now == 1000 + total_failures * capture.CAPTURE_POLL_MS / 1000
    heal.assert_not_called()


@pytest.mark.parametrize("exit_arm", ["deadline", "timeout", "navigation_error"])
def test_each_wait_recheck_requires_sid(monkeypatch: pytest.MonkeyPatch, exit_arm: str) -> None:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    browser = _Browser(cookies=[NID])
    _install_browser(monkeypatch, browser)
    if exit_arm == "timeout":
        browser.page.wait_for_url.side_effect = PlaywrightTimeout("synthetic timeout")
    elif exit_arm == "navigation_error":
        browser.page.wait_for_url.side_effect = PlaywrightError("net::ERR_ABORTED")
    error = PlaywrightError if exit_arm == "navigation_error" else PlaywrightTimeout
    with pytest.raises(error):
        capture.wait_for_login_landing(browser.page, timeout_s=0 if exit_arm == "deadline" else 3)


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize(
    "invalid_sid",
    [
        {**SID, "value": ""},
        {**SID, "expires": 1},
        {**SID, "domain": ".google.co.uk"},
        {**SID, "domain": ".example.com"},
        {**SID, "path": "/unrelated"},
        {**SID, "domain": "notebooklm.google.com"},
    ],
    ids=["empty", "expired", "regional", "wrong_domain", "wrong_path", "alias_only"],
)
def test_invalid_final_export_is_refused_before_heal_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    invalid_sid: dict[str, Any],
) -> None:
    browser = _Browser(cookies=[SID])
    browser.state["cookies"] = [invalid_sid]
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()

    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, _IO())

    heal.assert_not_called()
    writer.assert_not_called()
    assert plan.storage_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize("change", ["export_sid", "browser_sid", "browser_url"])
def test_late_invalid_session_after_heal_does_not_replace_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, change: str
) -> None:
    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)

    def invalidate(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if change == "export_sid":
            state = {"cookies": [], "origins": []}
        elif change == "browser_sid":
            browser.browser_cookies = [NID]
        else:
            browser.page.url = SIGN_IN
        return state, None

    heal.side_effect = invalidate
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, _IO())

    heal.assert_called_once()
    writer.assert_not_called()
    assert plan.storage_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize("new_url", [f"{APP}?authuser=0", f"{APP}notebook/synthetic"])
def test_sid_only_capture_survives_post_heal_app_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, new_url: str
) -> None:
    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    snapshots: list[str] = []

    def move_during_snapshot(urls: list[str]) -> list[dict[str, Any]]:
        assert urls == [browser.page.url]
        snapshots.append(urls[0])
        if len(snapshots) == 1:
            browser.page.url = new_url
        return [deepcopy(SID)]

    def decline_after_app_navigation(
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], ValueError]:
        browser.context.cookies.side_effect = move_during_snapshot
        return state, ValueError("synthetic missing PSIDTS")

    heal.side_effect = decline_after_app_navigation
    plan = _existing_plan(tmp_path)
    writer = MagicMock(wraps=capture.replace_captured_profile)
    monkeypatch.setattr(capture, "replace_captured_profile", writer)

    _run(mode, plan, _IO())

    assert snapshots == [APP, new_url]
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]
    writer.assert_called_once()
    heal.assert_called_once()
    browser.page.wait_for_timeout.assert_not_called()


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize("new_url", [f"{APP}?authuser=0", f"{APP}notebook/synthetic"])
def test_final_routing_verification_reobserves_a_changed_app_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, new_url: str
) -> None:
    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    original_routes = capture._auth_cookies._storage_has_routable_cookie
    routed_urls: list[str] = []

    def routes_with_navigation(state: dict[str, Any], name: str, url: str) -> bool:
        result = original_routes(state, name, url)
        routed_urls.append(url)
        if len(routed_urls) == 2:
            browser.page.url = new_url
        return result

    def decline_before_navigation(state: dict[str, Any]) -> tuple[dict[str, Any], ValueError]:
        monkeypatch.setattr(
            capture._auth_cookies, "_storage_has_routable_cookie", routes_with_navigation
        )
        return state, ValueError("synthetic missing PSIDTS")

    heal.side_effect = decline_before_navigation
    plan = _existing_plan(tmp_path)

    _run(mode, plan, _IO())

    assert routed_urls == [APP, APP, APP, new_url]
    assert [call.args[0] for call in browser.context.cookies.call_args_list[-2:]] == [
        [APP],
        [new_url],
    ]
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]
    heal.assert_called_once()
    browser.page.wait_for_timeout.assert_not_called()


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize("failure", ["off_host", "sid_lost", "unstable", "export_scope"])
def test_final_routing_reobservation_still_refuses_invalid_or_unstable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, failure: str
) -> None:
    stored_sid = {**SID, "domain": "notebook.google.com"} if failure == "export_scope" else SID
    browser = _Browser(cookies=[SID])
    browser.state["cookies"] = [deepcopy(stored_sid)]
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    original_routes = capture._auth_cookies._storage_has_routable_cookie
    routed_urls: list[str] = []
    snapshots: list[str] = []

    def cookies(urls: list[str]) -> list[dict[str, Any]]:
        snapshots.append(urls[0])
        return browser.cookies(urls)

    def routes_with_navigation(state: dict[str, Any], name: str, url: str) -> bool:
        result = original_routes(state, name, url)
        routed_urls.append(url)
        if len(routed_urls) % 2 == 0:
            if failure == "off_host":
                browser.page.url = SIGN_IN
            elif failure == "export_scope":
                browser.page.url = "https://notebooklm.google.com/"
            else:
                browser.page.url = f"{APP}?transition={len(routed_urls)}"
                if failure == "sid_lost":
                    browser.browser_cookies = [deepcopy(NID)]
        return result

    def decline_before_navigation(state: dict[str, Any]) -> tuple[dict[str, Any], ValueError]:
        browser.context.cookies.side_effect = cookies
        monkeypatch.setattr(
            capture._auth_cookies, "_storage_has_routable_cookie", routes_with_navigation
        )
        return state, ValueError("synthetic missing PSIDTS")

    heal.side_effect = decline_before_navigation
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, io)

    expected_snapshots = {"off_host": 1, "sid_lost": 2, "unstable": 3, "export_scope": 2}
    assert len(snapshots) == expected_snapshots[failure]
    assert len(routed_urls) <= 2 * capture.CAPTURE_SNAPSHOT_ATTEMPTS
    writer.assert_not_called()
    heal.assert_called_once()
    browser.page.wait_for_timeout.assert_not_called()
    assert plan.storage_path.read_bytes() == before
    if mode == "interactive":
        assert any(
            "Could not verify a stable Google cookie capture" in message for message in io.messages
        )


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize("failure", ["off_host", "sid_lost", "unstable"])
def test_post_heal_reobservation_refuses_an_invalid_or_unstable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, failure: str
) -> None:
    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    snapshots: list[str] = []

    def move_during_snapshot(urls: list[str]) -> list[dict[str, Any]]:
        assert urls == [browser.page.url]
        snapshots.append(urls[0])
        if failure == "off_host":
            browser.page.url = SIGN_IN
        elif failure == "unstable":
            browser.page.url = f"{APP}?transition={len(snapshots)}"
        elif len(snapshots) == 1:
            browser.page.url = f"{APP}?authuser=0"
        else:
            return [deepcopy(NID)]
        return [deepcopy(SID)]

    def decline_before_invalidation(
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], ValueError]:
        browser.context.cookies.side_effect = move_during_snapshot
        return state, ValueError("synthetic missing PSIDTS")

    heal.side_effect = decline_before_invalidation
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, _IO())

    expected_snapshots = {"off_host": 1, "sid_lost": 2, "unstable": 3}
    assert len(snapshots) == expected_snapshots[failure]
    assert plan.storage_path.read_bytes() == before
    writer.assert_not_called()
    heal.assert_called_once()
    browser.page.wait_for_timeout.assert_not_called()


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_current_host_sid_cannot_replace_legacy_rpc_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")
    browser = _Browser(cookies=[{**SID, "domain": "notebook.google.com"}])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, _IO())

    assert plan.storage_path.read_bytes() == before
    heal.assert_not_called()


def test_cookie_read_navigation_race_does_not_accept_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _Browser(cookies=[SID])

    def move_while_reading(urls: list[str]) -> list[dict[str, Any]]:
        browser.page.url = SIGN_IN
        return [SID]

    browser.context.cookies.side_effect = move_while_reading
    assert capture._capture_candidate_url(browser.page, browser.context) is None


def test_restored_sid_after_every_goto_aborts_is_not_commit_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[SID])
    browser.page.goto.side_effect = PlaywrightError("net::ERR_ABORTED")
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    with pytest.raises(_InteractiveExit):
        _run("interactive", plan, io)

    assert browser.page.goto.call_count == capture.LOGIN_MAX_RETRIES + 2
    writer.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before
    assert not any("saving cookies" in message for message in io.messages)
    assert not any("Authentication saved" in message for message in io.messages)
    messages = " ".join(io.messages)
    assert "Login navigation never committed" in messages
    assert "saved authentication was not replaced" in messages
    assert "Retry: notebooklm login" in messages
    assert "Unexpected URL" not in messages
    assert "--fresh" not in messages


@pytest.mark.parametrize("mode", ["interactive", "headless"])
def test_recovered_on_app_page_without_a_commit_reports_navigation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[SID])
    browser.page.goto.side_effect = [None, PlaywrightError(capture.TARGET_CLOSED_ERROR)]
    recovered = MagicMock()
    recovered.url = APP
    recovered.goto.side_effect = PlaywrightError("net::ERR_ABORTED")
    browser.context.new_page.return_value = recovered
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error) as exc_info:
        _run(mode, plan, io)

    assert recovered.goto.call_count == 2
    writer.assert_not_called()
    heal.assert_not_called()
    browser.context.storage_state.assert_not_called()
    assert plan.storage_path.read_bytes() == before
    messages = " ".join(io.messages)
    assert "Login navigation never committed" in messages
    assert "saved authentication was not replaced" in messages
    assert "Retry: notebooklm login" in messages
    assert "Unexpected URL" not in messages
    assert "--fresh" not in messages
    if mode == "headless":
        assert "Login navigation never committed" in str(exc_info.value)


def test_forcing_commits_after_initial_goto_races_permit_sid_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[SID])
    initial_attempts = 0

    def goto_after_initial_races(url: str, **kwargs: Any) -> None:
        nonlocal initial_attempts
        if initial_attempts < capture.LOGIN_MAX_RETRIES:
            initial_attempts += 1
            raise PlaywrightError("net::ERR_ABORTED")
        browser.goto(url, **kwargs)

    browser.page.goto.side_effect = goto_after_initial_races
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)

    _run("interactive", plan, _IO())

    assert browser.page.goto.call_count == capture.LOGIN_MAX_RETRIES + 2
    assert [call.args[0] for call in browser.page.goto.call_args_list[-2:]] == [
        capture.GOOGLE_ACCOUNTS_URL,
        APP,
    ]
    heal.assert_called_once()
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]


def test_initial_settle_and_fallback_share_a_short_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path, timeout=1)
    before = plan.storage_path.read_bytes()
    with pytest.raises(_InteractiveExit):
        _run("interactive", plan, _IO())

    assert browser.now == 1001
    assert browser.page.goto.call_count == 1
    assert browser.page.wait_for_timeout.call_count == 2
    browser.page.wait_for_url.assert_not_called()
    assert plan.storage_path.read_bytes() == before
    heal.assert_not_called()


def test_off_host_human_sign_in_is_not_redirected_to_service_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser(cookies=[NID])
    browser.finish_sign_in = True

    def goto(url: str, **kwargs: Any) -> None:
        browser.goto(url, **kwargs)
        if browser.app_visits == 1:
            browser.page.url = SIGN_IN

    browser.page.goto.side_effect = goto
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)

    _run("interactive", plan, _IO())

    assert all("ServiceLogin?" not in call.args[0] for call in browser.page.goto.call_args_list)
    browser.page.wait_for_timeout.assert_not_called()
    browser.page.wait_for_url.assert_called_once()
    heal.assert_called_once()


@pytest.mark.parametrize("step", ["cookie_read", "url_read"])
def test_courtesy_decision_does_not_interrupt_a_racing_sso_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    browser = _Browser(cookies=[NID])
    browser.finish_sign_in = True
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    io = _IO()
    moved_to_sso = False
    read_cookies = False
    cached_url = APP

    def page_url(*values: str) -> str | None:
        nonlocal moved_to_sso, cached_url
        if values:
            cached_url = values[0]
            return None
        observed = cached_url
        if step == "url_read" and read_cookies and not moved_to_sso:
            # The cookie snapshot's final URL read observes the old URL while
            # the page moves; the courtesy decision must recheck that snapshot.
            cached_url = SIGN_IN
            moved_to_sso = True
        return observed

    def cookies(urls: list[str]) -> list[dict[str, Any]]:
        nonlocal read_cookies, moved_to_sso
        rows = browser.cookies(urls)
        if any("Waiting for login" in message for message in io.messages) and not moved_to_sso:
            read_cookies = True
            if step == "cookie_read":
                browser.page.url = SIGN_IN
                moved_to_sso = True
        return rows

    human_wait_urls: list[str] = []

    def human_wait(matcher: Any, **kwargs: Any) -> None:
        human_wait_urls.append(browser.page.url)
        browser.wait_for_url(matcher, **kwargs)

    monkeypatch.setattr(
        type(browser.page), "url", PropertyMock(side_effect=page_url), raising=False
    )
    browser.context.cookies.side_effect = cookies
    browser.page.wait_for_url.side_effect = human_wait

    _run("interactive", plan, io)

    assert moved_to_sso
    assert all("ServiceLogin?" not in call.args[0] for call in browser.page.goto.call_args_list)
    assert human_wait_urls == [SIGN_IN]
    assert browser.page.wait_for_url.call_args.kwargs["timeout"] == 4000
    assert browser.now == 1002
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]
    heal.assert_called_once()


@pytest.mark.parametrize("transition", ["sid_arrival", "url_churn"])
def test_courtesy_decision_declines_sid_arrival_and_unstable_app_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    io = _IO()
    courtesy_snapshots = 0

    def cookies(urls: list[str]) -> list[dict[str, Any]]:
        nonlocal courtesy_snapshots
        rows = browser.cookies(urls)
        if (
            any("Waiting for login" in message for message in io.messages)
            and not browser.page.wait_for_url.called
        ):
            courtesy_snapshots += 1
            if transition == "sid_arrival":
                browser.browser_cookies = [deepcopy(SID)]
                browser.state["cookies"] = [deepcopy(SID)]
                return deepcopy(browser.browser_cookies)
            browser.page.url = f"{APP}?transition={courtesy_snapshots}"
        return rows

    def human_wait(matcher: Any, **kwargs: Any) -> None:
        browser.page.url = APP
        browser.browser_cookies = [deepcopy(SID)]
        browser.state["cookies"] = [deepcopy(SID)]
        browser.wait_for_url(matcher, **kwargs)

    browser.context.cookies.side_effect = cookies
    browser.page.wait_for_url.side_effect = human_wait

    _run("interactive", plan, io)

    assert courtesy_snapshots == (1 if transition == "sid_arrival" else 3)
    assert all("ServiceLogin?" not in call.args[0] for call in browser.page.goto.call_args_list)
    browser.page.wait_for_url.assert_called_once()
    assert browser.page.wait_for_url.call_args.kwargs["timeout"] == 4000
    assert browser.now == 1002
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]
    heal.assert_called_once()


def test_courtesy_observation_consuming_deadline_never_schedules_a_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    consumed_deadline = False

    def cookies(urls: list[str]) -> list[dict[str, Any]]:
        nonlocal consumed_deadline
        rows = browser.cookies(urls)
        if any("Waiting for login" in message for message in io.messages) and not consumed_deadline:
            browser.now += 4
            consumed_deadline = True
        return rows

    browser.context.cookies.side_effect = cookies

    with pytest.raises(_InteractiveExit):
        _run("interactive", plan, io)

    assert consumed_deadline
    assert browser.now == 1006
    assert any("Login not detected" in message for message in io.messages)
    browser.page.goto.assert_called_once()
    browser.page.wait_for_url.assert_not_called()
    writer.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before


@pytest.mark.parametrize("step", ["cookie_read", "url_read"])
def test_browser_close_during_courtesy_observation_retains_abort_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    cached_url = APP

    def page_url(*values: str) -> str | None:
        nonlocal cached_url
        if values:
            cached_url = values[0]
            return None
        if step == "url_read" and any("Waiting for login" in message for message in io.messages):
            raise PlaywrightError(capture.TARGET_CLOSED_ERROR)
        return cached_url

    def cookies(urls: list[str]) -> list[dict[str, Any]]:
        if step == "cookie_read" and any("Waiting for login" in message for message in io.messages):
            raise PlaywrightError(capture.TARGET_CLOSED_ERROR)
        return browser.cookies(urls)

    monkeypatch.setattr(
        type(browser.page), "url", PropertyMock(side_effect=page_url), raising=False
    )
    browser.context.cookies.side_effect = cookies

    with pytest.raises(_InteractiveExit):
        _run("interactive", plan, io)

    assert any("browser window was closed" in message for message in io.messages)
    assert all("ServiceLogin?" not in call.args[0] for call in browser.page.goto.call_args_list)
    browser.page.wait_for_url.assert_not_called()
    writer.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
@pytest.mark.parametrize("step", ["cookie_read", "settle_wait"])
def test_browser_closed_during_candidate_settle_retains_abort_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, step: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])
    closed = PlaywrightError(capture.TARGET_CLOSED_ERROR)
    if step == "cookie_read":
        browser.context.cookies.side_effect = closed
    else:
        browser.page.wait_for_timeout.side_effect = closed
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    error = _InteractiveExit if mode == "interactive" else capture._HeadlessCaptureAbort
    with pytest.raises(error) as exc_info:
        _run(mode, plan, io)

    if mode != "interactive":
        assert exc_info.value.kind is capture._CaptureAbortKind.BROWSER_CLOSED
    assert plan.storage_path.read_bytes() == before
    heal.assert_not_called()
    if mode == "interactive":
        assert any("browser window was closed" in message for message in io.messages)


@pytest.mark.parametrize(
    ("mode", "phase"),
    [
        ("interactive", "candidate"),
        ("headless", "candidate"),
        ("cdp", "candidate"),
        ("interactive", "commit"),
        ("headless", "commit"),
    ],
)
def test_url_read_target_closed_retains_browser_abort_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, phase: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[SID])
    closed = PlaywrightError(capture.TARGET_CLOSED_ERROR)

    def close_before_url_read(url: str, **kwargs: Any) -> None:
        browser.goto(url, **kwargs)
        if phase == "candidate" or browser.app_visits == 2:
            monkeypatch.setattr(
                type(browser.page), "url", PropertyMock(side_effect=closed), raising=False
            )

    browser.page.goto.side_effect = close_before_url_read
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    error = _InteractiveExit if mode == "interactive" else capture._HeadlessCaptureAbort
    with pytest.raises(error) as exc_info:
        _run(mode, plan, io)

    if mode != "interactive":
        assert exc_info.value.kind is capture._CaptureAbortKind.BROWSER_CLOSED
    assert plan.storage_path.read_bytes() == before
    writer.assert_not_called()
    heal.assert_not_called()
    browser.context.storage_state.assert_not_called()
    assert capture.safe_page_url(browser.page) == capture._UNREADABLE_URL
    if mode == "interactive":
        assert any("browser window was closed" in message for message in io.messages)


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_page_only_close_after_heal_refuses_cached_url_and_context_sid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    snapshots_before_close = 0

    def close_after_heal(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        nonlocal snapshots_before_close
        snapshots_before_close = browser.context.cookies.call_count
        browser.page.is_closed.return_value = True
        return state, None

    heal.side_effect = close_after_heal
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    error = _InteractiveExit if mode == "interactive" else capture._HeadlessCaptureAbort
    with pytest.raises(error) as exc_info:
        _run(mode, plan, io)

    if mode != "interactive":
        assert exc_info.value.kind is capture._CaptureAbortKind.BROWSER_CLOSED
    assert plan.storage_path.read_bytes() == before
    writer.assert_not_called()
    heal.assert_called_once()
    assert browser.context.cookies.call_count == snapshots_before_close
    assert browser.page.url == APP
    assert browser.context.cookies([APP]) == [SID]
    assert capture.safe_page_url(browser.page) == APP
    if mode == "interactive":
        assert any("browser window was closed" in message for message in io.messages)


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_browser_closed_during_post_heal_guard_preserves_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)

    def close_after_heal(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        browser.context.cookies.side_effect = PlaywrightError(capture.TARGET_CLOSED_ERROR)
        return state, None

    heal.side_effect = close_after_heal
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    error = _InteractiveExit if mode == "interactive" else capture._HeadlessCaptureAbort
    with pytest.raises(error) as exc_info:
        _run(mode, plan, io)

    if mode != "interactive":
        assert exc_info.value.kind is capture._CaptureAbortKind.BROWSER_CLOSED
    assert plan.storage_path.read_bytes() == before
    writer.assert_not_called()
    heal.assert_called_once()
    if mode == "interactive":
        assert any("browser window was closed" in message for message in io.messages)


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_non_navigation_candidate_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])
    browser.context.cookies.side_effect = PlaywrightError("Protocol error: synthetic")
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    with pytest.raises(PlaywrightError, match="Protocol error"):
        _run(mode, plan, _IO())

    assert plan.storage_path.read_bytes() == before
    heal.assert_not_called()


@pytest.mark.parametrize("mode", ["interactive", "headless"])
def test_logout_during_cookie_forcing_preserves_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    browser = _Browser(cookies=[SID])
    browser.finish_cookie_forcing = False
    heal = _install_browser(monkeypatch, browser)
    monkeypatch.setattr(capture, "heal_captured_state", heal)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, _IO())

    assert plan.storage_path.read_bytes() == before
    heal.assert_not_called()
