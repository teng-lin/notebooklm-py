"""Contract tests for the coarse browser operations on ``notebooklm.auth``."""

from __future__ import annotations

import builtins
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import notebooklm.auth as auth
from notebooklm._auth import cookie_policy, storage
from notebooklm._browser import browser_capture, headless_reauth, oauth_token


def _fail(code: int) -> None:
    raise SystemExit(code)


def test_browser_channels_are_immutable_and_do_not_import_playwright(monkeypatch) -> None:
    imported: list[str] = []
    real_import = __import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("playwright"):
            imported.append(name)
            raise AssertionError("channel discovery imported Playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    channels = auth.browser_login_channels()

    assert isinstance(channels, tuple)
    assert channels == (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome"))
    assert imported == []


def test_availability_delegates_through_facade_io(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    emitted: list[str] = []

    def ensure(io: Any, *, browser: str) -> None:
        seen.update(io=io, browser=browser)
        io.emit("available")

    monkeypatch.setattr(browser_capture, "ensure_playwright_available", ensure)
    auth.ensure_browser_login_available("chrome", emit=emitted.append, fail=_fail)

    assert seen["browser"] == "chrome"
    assert emitted == ["available"]
    with pytest.raises(AssertionError, match="must not invoke"):
        seen["io"].run_async(None)


def test_capture_delegates_plan_and_projects_page_html(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    emitted: list[str] = []

    def capture(plan: Any, io: Any, *, headless: bool, interactive: bool) -> Any:
        seen.update(plan=plan, io=io, headless=headless, interactive=interactive)
        io.emit("capturing")
        return browser_capture.CaptureResult(page_html="<html>account</html>")

    monkeypatch.setattr(browser_capture, "run_browser_capture", capture)
    result = auth.run_browser_login_capture(
        browser="msedge",
        browser_profile=tmp_path / "browser",
        storage_path=tmp_path / "storage.json",
        include_domains={"youtube"},
        login_timeout_s=42,
        emit=emitted.append,
        fail=_fail,
    )

    assert result == "<html>account</html>"
    assert seen["headless"] is False
    assert seen["interactive"] is True
    assert seen["plan"] == browser_capture.BrowserCapturePlan(
        browser="msedge",
        browser_profile=tmp_path / "browser",
        storage_path=tmp_path / "storage.json",
        include_domains={"youtube"},
        login_timeout_s=42,
    )
    assert emitted == ["capturing"]


def test_readiness_is_projected_to_a_transport_neutral_tuple(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "browser"
    monkeypatch.setattr(
        headless_reauth,
        "headless_reauth_readiness",
        lambda *, browser_profile: SimpleNamespace(available=True, detail=str(browser_profile)),
    )

    assert auth.check_headless_reauth_readiness(browser_profile=profile) == (True, str(profile))


def test_oauth_capture_delegates_and_scrubs_facade_frame(monkeypatch) -> None:
    secret_endpoint = "http://user:secret@localhost:9222"
    error = RuntimeError("capture failed")

    def fail_capture(**kwargs: Any) -> str:
        assert kwargs == {
            "browser": "chrome",
            "cdp_url": secret_endpoint,
            "timeout_s": 17.0,
        }
        raise error

    monkeypatch.setattr(oauth_token, "capture_oauth_token", fail_capture)
    with pytest.raises(RuntimeError) as caught:
        auth.capture_browser_oauth_token(
            browser="chrome",
            cdp_url=secret_endpoint,
            timeout_s=17.0,
        )

    facade_frames = [
        frame_info.frame
        for frame_info in inspect.getinnerframes(caught.value.__traceback__)
        if frame_info.frame.f_code.co_name == "capture_browser_oauth_token"
    ]
    assert len(facade_frames) == 1
    assert {"browser", "cdp_url", "timeout_s"}.isdisjoint(facade_frames[0].f_locals)


def test_eager_browser_policy_aliases_preserve_identity() -> None:
    assert auth.app_host_scope_note is cookie_policy.app_host_scope_note
    assert (
        auth.filter_storage_state_cookies_by_domain_policy
        is storage.filter_storage_state_cookies_by_domain_policy
    )
