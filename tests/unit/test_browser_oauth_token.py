"""Direct contracts for visible-browser OAuth token capture."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import notebooklm._browser.oauth_token as capture_service
from notebooklm.auth import MasterTokenError


def _assert_owned_frame_scrubbed(error: BaseException, frame_name: str, names: set[str]) -> None:
    owned_frames = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_name == frame_name:
            owned_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert len(owned_frames) == 1
    assert not names & set(owned_frames[0])


def test_capture_missing_browser_extra_preserves_explicit_cause_and_scrubs(monkeypatch):
    import_failure = ImportError("playwright unavailable")
    original_import = builtins.__import__

    def reject_playwright(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise import_failure
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_playwright)
    secret_cdp = "http://user:MISSING-EXTRA-CDP-SECRET@localhost:9222"

    with pytest.raises(MasterTokenError) as raised:
        capture_service.capture_oauth_token(cdp_url=secret_cdp)

    assert raised.value.__cause__ is import_failure
    assert raised.value.__context__ is import_failure
    assert raised.value.__suppress_context__ is True
    assert str(raised.value) == (
        "Browser-assisted oauth_token capture needs the [browser] extra "
        "(pip install 'notebooklm-py[browser]'), or pass --oauth-token manually."
    )
    assert "MISSING-EXTRA-CDP-SECRET" not in str(raised.value)
    _assert_owned_frame_scrubbed(
        raised.value,
        "capture_oauth_token",
        {
            "browser",
            "cdp_url",
            "parsed_cdp",
            "playwright_driver",
            "browser_obj",
            "context",
            "page",
            "cookie",
            "token",
        },
    )


def test_capture_timeout_preserves_active_context_and_scrubs_resources(monkeypatch):
    page = Mock()
    page.evaluate.return_value = 0
    context = Mock()
    context.new_page.return_value = page
    browser_obj = Mock()
    browser_obj.new_context.return_value = context
    chromium = SimpleNamespace(launch=Mock(return_value=browser_obj))

    @contextmanager
    def playwright_context():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(capture_service, "sync_playwright_context", playwright_context)
    outer_error = LookupError("unrelated outer error")

    try:
        raise outer_error
    except LookupError as active:
        retained_outer = active
        with pytest.raises(MasterTokenError) as raised:
            capture_service.capture_oauth_token(timeout_s=0)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is retained_outer
    assert raised.value.__suppress_context__ is False
    assert str(raised.value).startswith("Did not observe an oauth_token cookie.")
    page.close.assert_called_once_with()
    context.close.assert_called_once_with()
    browser_obj.close.assert_called_once_with()
    _assert_owned_frame_scrubbed(
        raised.value,
        "capture_oauth_token",
        {
            "browser",
            "cdp_url",
            "parsed_cdp",
            "playwright_driver",
            "browser_obj",
            "context",
            "page",
            "cookie",
            "token",
        },
    )


@pytest.mark.parametrize(
    "cdp_url",
    [
        "http://user:PWSECRET42@localhost:9222",
        "https://localhost:9222/devtools?token=QSECRET42",
        "https://localhost:9222/devtools#FSECRET42",
        "http://[MALFORMEDSECRET42?token=secret",
        "http://remote-host:9222",
    ],
)
def test_capture_rejects_unsafe_cdp_before_connector(monkeypatch, cdp_url):
    context = Mock()
    monkeypatch.setattr(capture_service, "sync_playwright_context", context)

    with pytest.raises(MasterTokenError) as raised:
        capture_service.capture_oauth_token(cdp_url=cdp_url)

    assert str(raised.value) == (
        "CDP URL must be a credential-free loopback scheme/host/path endpoint without "
        "userinfo, query, or fragment."
    )
    assert all(
        sentinel not in str(raised.value)
        for sentinel in ("PWSECRET42", "QSECRET42", "FSECRET42", "MALFORMEDSECRET42")
    )
    context.assert_not_called()


def test_capture_rejected_cdp_scrubs_retained_frame_and_exception_chain(monkeypatch):
    sentinel = "RETAINED-CDP-SECRET-9f3a"
    context = Mock()
    monkeypatch.setattr(capture_service, "sync_playwright_context", context)

    with pytest.raises(MasterTokenError) as raised:
        capture_service.capture_oauth_token(
            cdp_url=f"https://localhost:9222/devtools?token={sentinel}"
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    current_error: BaseException | None = raised.value
    while current_error is not None:
        assert sentinel not in repr(current_error.args)
        current_error = current_error.__cause__ or current_error.__context__
    _assert_owned_frame_scrubbed(
        raised.value,
        "capture_oauth_token",
        {
            "browser",
            "cdp_url",
            "parsed_cdp",
            "playwright_driver",
            "browser_obj",
            "context",
            "page",
            "cookie",
            "token",
        },
    )
    context.assert_not_called()


def test_capture_passes_valid_cdp_unchanged_and_preserves_connector_error(monkeypatch):
    cdp_url = "http://localhost:9222/devtools/browser/opaque-path"
    connector_error = RuntimeError("connector failed")
    chromium = SimpleNamespace(connect_over_cdp=Mock(side_effect=connector_error))

    @contextmanager
    def playwright_context():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(capture_service, "sync_playwright_context", playwright_context)

    with pytest.raises(RuntimeError) as raised:
        capture_service.capture_oauth_token(cdp_url=cdp_url)

    assert raised.value is connector_error
    chromium.connect_over_cdp.assert_called_once_with(cdp_url)
    current = connector_error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_name == "capture_oauth_token":
            assert "cdp_url" not in current.tb_frame.f_locals
            assert "parsed_cdp" not in current.tb_frame.f_locals
        current = current.tb_next


@pytest.mark.parametrize("existing_context", [True, False])
def test_capture_cdp_preserves_external_ownership_and_closes_created_resources(
    monkeypatch, existing_context
):
    page = Mock()
    page.evaluate.return_value = 0
    context = Mock()
    context.new_page.return_value = page
    context.cookies.return_value = [{"name": "oauth_token", "value": "CAPTURED"}]
    browser_obj = Mock()
    browser_obj.contexts = [context] if existing_context else []
    browser_obj.new_context.return_value = context
    chromium = SimpleNamespace(connect_over_cdp=Mock(return_value=browser_obj))

    @contextmanager
    def playwright_context():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(capture_service, "sync_playwright_context", playwright_context)

    assert (
        capture_service.capture_oauth_token(
            cdp_url="http://localhost:9222/devtools/browser/id", timeout_s=1
        )
        == "CAPTURED"
    )
    page.close.assert_called_once_with()
    browser_obj.close.assert_not_called()
    if existing_context:
        browser_obj.new_context.assert_not_called()
        context.close.assert_not_called()
    else:
        browser_obj.new_context.assert_called_once_with()
        context.close.assert_called_once_with()


def test_capture_local_launch_closes_owned_page_context_and_browser(monkeypatch):
    page = Mock()
    page.evaluate.return_value = 0
    context = Mock()
    context.new_page.return_value = page
    context.cookies.return_value = [{"name": "oauth_token", "value": "CAPTURED"}]
    browser_obj = Mock()
    browser_obj.new_context.return_value = context
    chromium = SimpleNamespace(launch=Mock(return_value=browser_obj))

    @contextmanager
    def playwright_context():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(capture_service, "sync_playwright_context", playwright_context)

    assert capture_service.capture_oauth_token(browser="chrome", timeout_s=1) == "CAPTURED"
    chromium.launch.assert_called_once_with(
        headless=False,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page.close.assert_called_once_with()
    context.close.assert_called_once_with()
    browser_obj.close.assert_called_once_with()


def test_capture_cleanup_failure_preserves_parent_precedence(monkeypatch):
    cleanup_failure = RuntimeError("page close")
    page = Mock()
    page.evaluate.return_value = 0
    page.close.side_effect = cleanup_failure
    context = Mock()
    context.new_page.return_value = page
    context.cookies.return_value = [{"name": "oauth_token", "value": "CAPTURED"}]
    browser_obj = Mock()
    browser_obj.new_context.return_value = context
    chromium = SimpleNamespace(launch=Mock(return_value=browser_obj))

    @contextmanager
    def playwright_context():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(capture_service, "sync_playwright_context", playwright_context)

    with pytest.raises(RuntimeError) as raised:
        capture_service.capture_oauth_token(timeout_s=1)

    assert raised.value is cleanup_failure
    context.close.assert_not_called()
    browser_obj.close.assert_not_called()
