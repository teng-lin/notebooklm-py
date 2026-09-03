"""Behavior tests for transport-neutral browser-login orchestration."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

import notebooklm._app.login_browser as login_browser


def _fail(code: int) -> None:
    raise SystemExit(code)


def test_run_browser_login_orders_availability_preflight_events_capture_and_repair(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, Any]] = []
    plan = login_browser.BrowserLoginPlan(
        browser="chromium",
        browser_profile=tmp_path / "browser",
        storage_path=tmp_path / "storage.json",
        include_domains={"youtube"},
        login_timeout_s=41,
    )

    monkeypatch.setattr(
        login_browser.auth,
        "ensure_browser_login_available",
        lambda browser, **_: calls.append(("availability", browser)),
    )
    monkeypatch.setattr(
        login_browser.auth,
        "run_browser_login_capture",
        lambda **kwargs: calls.append(("capture", kwargs)) or "<html>account</html>",
    )
    monkeypatch.setattr(
        login_browser,
        "browser_login_channels",
        lambda: (("chromium", "Chromium"),),
    )
    monkeypatch.setattr(login_browser, "resolve_profile", lambda: "default")
    monkeypatch.setattr(
        login_browser,
        "repair_playwright_account_metadata",
        lambda storage_path, **kwargs: calls.append(
            ("repair", (storage_path, kwargs["page_html"]))
        ),
    )

    login_browser.run_browser_login(
        plan,
        emit_event=lambda event: calls.append(("event", event.kind)),
        browser_emit=lambda *args, **kwargs: None,
        fail=_fail,
        run_async=lambda operation: operation,
        chromium_preflight=lambda: calls.append(("preflight", None)),
    )

    assert [name for name, _ in calls] == [
        "availability",
        "preflight",
        "event",
        "event",
        "event",
        "capture",
        "repair",
    ]
    assert [value for name, value in calls if name == "event"] == [
        "PROFILE",
        "OPENING_BROWSER",
        "BROWSER_PROFILE",
    ]
    capture = next(value for name, value in calls if name == "capture")
    assert capture["browser"] == "chromium"
    assert capture["browser_profile"] == plan.browser_profile
    assert capture["storage_path"] == plan.storage_path
    assert capture["include_domains"] is plan.include_domains
    assert capture["login_timeout_s"] == 41
    assert calls[-1] == ("repair", (plan.storage_path, "<html>account</html>"))


def test_missing_browser_extra_fails_before_preflight_or_rendering(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def unavailable(browser: str, **kwargs: Any) -> None:
        calls.append("availability")
        kwargs["fail"](1)

    monkeypatch.setattr(login_browser.auth, "ensure_browser_login_available", unavailable)
    plan = login_browser.BrowserLoginPlan(
        browser="chromium",
        browser_profile=tmp_path / "browser",
        storage_path=tmp_path / "storage.json",
    )

    with pytest.raises(SystemExit) as caught:
        login_browser.run_browser_login(
            plan,
            emit_event=lambda event: calls.append("event"),
            browser_emit=lambda *args, **kwargs: calls.append("browser_emit"),
            fail=_fail,
            run_async=lambda operation: operation,
            chromium_preflight=lambda: calls.append("preflight"),
        )

    assert caught.value.code == 1
    assert calls == ["availability"]


def test_system_browser_channel_skips_chromium_preflight(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        login_browser.auth,
        "ensure_browser_login_available",
        lambda browser, **kwargs: calls.append("availability"),
    )
    monkeypatch.setattr(
        login_browser.auth,
        "run_browser_login_capture",
        lambda **kwargs: calls.append("capture") or None,
    )
    monkeypatch.setattr(
        login_browser,
        "repair_playwright_account_metadata",
        lambda storage_path, **kwargs: calls.append("repair"),
    )
    monkeypatch.setattr(login_browser, "resolve_profile", lambda: "default")
    plan = login_browser.BrowserLoginPlan(
        browser="chrome",
        browser_profile=tmp_path / "browser",
        storage_path=tmp_path / "storage.json",
    )

    login_browser.run_browser_login(
        plan,
        emit_event=lambda event: None,
        browser_emit=lambda *args, **kwargs: None,
        fail=_fail,
        run_async=lambda operation: operation,
        chromium_preflight=lambda: calls.append("preflight"),
    )

    assert calls == ["availability", "capture", "repair"]


def test_run_browser_login_scrubs_captured_page_html_from_failure_frame(
    monkeypatch, tmp_path: Path
) -> None:
    secret_html = "<html>credential-bearing-account-page</html>"
    error = RuntimeError("repair failed")
    monkeypatch.setattr(
        login_browser.auth,
        "ensure_browser_login_available",
        lambda browser, **kwargs: None,
    )
    monkeypatch.setattr(
        login_browser.auth,
        "run_browser_login_capture",
        lambda **kwargs: secret_html,
    )
    monkeypatch.setattr(
        login_browser,
        "repair_playwright_account_metadata",
        lambda storage_path, **kwargs: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(login_browser, "resolve_profile", lambda: "default")
    plan = login_browser.BrowserLoginPlan(
        browser="chrome",
        browser_profile=tmp_path / "browser",
        storage_path=tmp_path / "storage.json",
    )

    with pytest.raises(RuntimeError) as caught:
        login_browser.run_browser_login(
            plan,
            emit_event=lambda event: None,
            browser_emit=lambda *args, **kwargs: None,
            fail=_fail,
            run_async=lambda operation: operation,
            chromium_preflight=lambda: None,
        )

    assert caught.value is error
    frames = [
        frame_info.frame
        for frame_info in inspect.getinnerframes(caught.value.__traceback__)
        if frame_info.frame.f_code.co_name == "run_browser_login"
    ]
    assert len(frames) == 1
    assert "page_html" not in frames[0].f_locals


def test_app_module_contains_no_cli_markup() -> None:
    source = Path(login_browser.__file__).read_text(encoding="utf-8")
    assert "[red]" not in source
    assert "[yellow]" not in source
    assert "rich" not in source.lower()
