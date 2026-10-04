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
from unittest.mock import MagicMock
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

    monkeypatch.setattr(capture, "sync_playwright_context", playwright_context)
    monkeypatch.setattr(capture.time, "monotonic", lambda: browser.now)
    heal = MagicMock(side_effect=lambda state: (state, ValueError("synthetic missing PSIDTS")))
    monkeypatch.setattr(capture, "heal_captured_state", heal)
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


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_same_document_sid_arrival_during_settle_is_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    browser = _Browser(cookies=[NID])
    browser.arrive_at = browser.now + 1
    heal = _install_browser(monkeypatch, browser)
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

    assert capture.wait_for_login_landing(browser.page, timeout_s=3) == 0

    assert browser.now == 1001
    assert browser.page.wait_for_timeout.call_count == 2
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
@pytest.mark.parametrize("failure", ["off_host", "sid_lost", "unstable"])
def test_post_heal_reobservation_refuses_an_invalid_or_unstable_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, failure: str
) -> None:
    browser = _Browser(cookies=[SID])
    heal = _install_browser(monkeypatch, browser)
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
    writer = MagicMock()
    monkeypatch.setattr(capture, "replace_captured_profile", writer)
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    io = _IO()
    with pytest.raises(_InteractiveExit):
        _run("interactive", plan, io)

    assert browser.page.goto.call_count == capture.LOGIN_MAX_RETRIES
    writer.assert_not_called()
    heal.assert_not_called()
    assert plan.storage_path.read_bytes() == before
    assert not any("Capturing Google cookies" in message for message in io.messages)
    browser.page.remove_listener.assert_called_once()


def test_same_url_commit_after_initial_goto_races_permits_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[SID])
    browser.page.goto.side_effect = PlaywrightError("net::ERR_ABORTED")
    listeners: list[Any] = []
    browser.page.on.side_effect = lambda event, listener: listeners.append(listener)

    def commit_same_url(*args: Any, **kwargs: Any) -> None:
        for listener in listeners:
            listener(browser.page.main_frame)

    browser.page.wait_for_url.side_effect = commit_same_url
    heal = _install_browser(monkeypatch, browser)
    plan = _existing_plan(tmp_path)

    _run("interactive", plan, _IO())

    heal.assert_called_once()
    assert json.loads(plan.storage_path.read_text())["cookies"] == [SID]


def test_initial_settle_and_fallback_share_a_short_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = _Browser(cookies=[NID])
    heal = _install_browser(monkeypatch, browser)
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
    plan = _existing_plan(tmp_path)

    _run("interactive", plan, _IO())

    assert all("ServiceLogin?" not in call.args[0] for call in browser.page.goto.call_args_list)
    browser.page.wait_for_timeout.assert_not_called()
    browser.page.wait_for_url.assert_called_once()
    heal.assert_called_once()


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


@pytest.mark.parametrize("mode", ["interactive", "headless", "cdp"])
def test_non_navigation_candidate_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    browser = _Browser(cookies=[NID])
    browser.context.cookies.side_effect = PlaywrightError("Protocol error: synthetic")
    heal = _install_browser(monkeypatch, browser)
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
    plan = _existing_plan(tmp_path)
    before = plan.storage_path.read_bytes()
    error = _InteractiveExit if mode == "interactive" else HeadlessLoginRequiredError
    with pytest.raises(error):
        _run(mode, plan, _IO())

    assert plan.storage_path.read_bytes() == before
    heal.assert_not_called()
