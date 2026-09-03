"""Unit tests for the interactive login-wait DEBUG tracing (#2046).

``notebooklm -vv login`` used to emit output byte-identical to plain
``notebooklm login`` for the entire five-minute ``page.wait_for_url`` block, so
a login that never landed (the ``notebook.google.com`` rebrand) looked exactly
like a user who walked away. These tests pin the three properties that make the
new tracing safe to run inside that wait:

* it is **inert** when DEBUG is off (no listener attached at all);
* it **cannot break the wait** (every listener failure is swallowed, and the
  listener is always detached);
* it **never logs credentials** — every URL goes through ``_safe_url``, so the
  query, fragment, userinfo, and Google-OAuth path are gone before the record
  is created.

The last one is the important one: login-flow URLs routinely carry ``f.sid``,
``continue=``, and OAuth grant material, and ``-vv`` output is exactly what our
issue template asks users to paste into a public bug report.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from notebooklm._browser.browser_capture import (
    BrowserCapturePlan,
    accepted_login_hosts,
    log_observed_navigations,
    run_browser_capture,
    safe_page_url,
    trace_url,
    url_matches_base_host,
)

# The tracing used to live in ``_auth/login_wait_trace.py``, a leaf that existed
# only to keep the capture core under the ADR-0008 line cap. ADR-0033 PR 4.1
# absorbed it into ``browser_capture``, which is now both the definition site and
# the logger these records carry — a logger follows its defining module, so the
# two names below are deliberately the same string. They are kept as separate
# constants because the assertions they gate mean different things: TRACE_LOGGER
# marks "emitted by the tracing helpers", CAPTURE_LOGGER "emitted by the capture
# core around the wait".
TRACE_LOGGER = "notebooklm._browser.browser_capture"
CAPTURE_LOGGER = "notebooklm._browser.browser_capture"


class _FakePage:
    """Minimal Playwright ``Page`` stand-in that records listener bookkeeping."""

    def __init__(self, *, main_frame: Any = None) -> None:
        self.main_frame = main_frame if main_frame is not None else MagicMock()
        self.listeners: list[tuple[str, Any]] = []
        self.removed: list[tuple[str, Any]] = []

    def on(self, event: str, handler: Any) -> None:
        self.listeners.append((event, handler))

    def remove_listener(self, event: str, handler: Any) -> None:
        self.removed.append((event, handler))

    def navigate(self, frame: Any) -> None:
        """Fire every registered ``framenavigated`` handler."""
        for event, handler in self.listeners:
            if event == "framenavigated":
                handler(frame)


def _frame(url: str) -> Any:
    frame = MagicMock()
    frame.url = url
    return frame


@pytest.fixture
def debug_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Enable DEBUG on the package logger, exactly as ``-vv`` does."""
    caplog.set_level(logging.DEBUG, logger="notebooklm")
    return caplog


# ---------------------------------------------------------------------------
# accepted_login_hosts: the accept set the wait message names
# ---------------------------------------------------------------------------


def test_accepted_hosts_include_the_rebranded_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    # Exact set, not membership: this pins that the alias is present AND that
    # nothing else crept into the accept set.
    assert set(accepted_login_hosts()) == {"notebooklm.google.com", "notebook.google.com"}


def test_accepted_hosts_drive_the_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The advertised accept set must BE the predicate, not a parallel copy."""
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    for host in accepted_login_hosts():
        assert url_matches_base_host(f"https://{host}/some/path")
    assert not url_matches_base_host("https://accounts.google.com/")


@pytest.mark.parametrize(
    "selected", ["https://notebook.google.com", "https://notebooklm.google.com"]
)
def test_either_personal_host_accepts_both(monkeypatch: pytest.MonkeyPatch, selected: str) -> None:
    """Pinning *either* personal host must still accept the other one.

    Google's login can land on the legacy host or the post-rebrand alias
    regardless of which one we navigated to. Keying the accept set on the
    selected host alone would reject a good landing — and would make a rollback
    to (or a future default flip toward) the other host un-loginnable, which is
    precisely the escape hatch this pair of hosts exists to preserve.
    """
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", selected)

    hosts = accepted_login_hosts()
    assert set(hosts) == {"notebooklm.google.com", "notebook.google.com"}
    # The selected host leads, so the login-wait DEBUG line names the host we
    # actually navigated to first; the rest is ordered, not arbitrary.
    assert hosts[0] == selected.removeprefix("https://")
    assert len(hosts) == len(set(hosts))

    for host in ("notebooklm.google.com", "notebook.google.com"):
        assert url_matches_base_host(f"https://{host}/notebook/abc")
    assert not url_matches_base_host("https://notebooklm.cloud.google.com/")


def test_enterprise_host_has_no_personal_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

    assert accepted_login_hosts() == ("notebooklm.cloud.google.com",)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://notebook.google.com/", True),
        ("https://notebooklm.google.com/", True),
        # Case-insensitive: the predicate lowercases the parsed hostname.
        ("https://NoteBookLM.Google.COM/", True),
        ("https://NOTEBOOK.GOOGLE.COM/x", True),
        ("https://accounts.google.com/signin", False),
        # A near-miss suffix must not match.
        ("https://evil-notebook.google.com.attacker.test/", False),
        # No hostname at all — must not fall through to a match.
        ("", False),
        ("not-a-url", False),
        ("file:///tmp/x", False),
    ],
)
def test_predicate_semantics_unchanged_by_the_helper_extraction(
    monkeypatch: pytest.MonkeyPatch, url: str, expected: bool
) -> None:
    """``accepted_login_hosts`` refactored the predicate — pin its full truth table."""
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    assert url_matches_base_host(url) is expected


# ---------------------------------------------------------------------------
# trace_url: host-only rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Path dropped even on the product host.
        ("https://notebook.google.com/notebook/abc123", "https://notebook.google.com/"),
        # Query and fragment dropped.
        ("https://accounts.google.com/signin?continue=SECRET", "https://accounts.google.com/"),
        ("https://notebook.google.com/#access_token=SECRET", "https://notebook.google.com/"),
        # Userinfo dropped — rebuilt from hostname, never netloc.
        ("https://SECRET@notebooklm.google.com/", "https://notebooklm.google.com/"),
        # A non-standard port is operator signal and survives.
        ("http://localhost:8080/x", "http://localhost:8080/"),
        # IPv6 literals keep their brackets — ``urlparse.hostname`` strips them,
        # and without them the port merges into the address's last group.
        ("https://[2001:db8::1]:8443/path?x=1", "https://[2001:db8::1]:8443/"),
        ("https://[2001:db8::1]/path", "https://[2001:db8::1]/"),
        # Host is lowercased by urlparse's hostname.
        ("https://NoteBook.Google.COM/x", "https://notebook.google.com/"),
        # Hostless URLs keep only the scheme.
        ("about:blank", "about:<no host>"),
        ("data:text/html;base64,U0VDUkVU", "data:<no host>"),
        ("", ""),
    ],
)
def test_trace_url_keeps_only_the_host(raw: str, expected: str) -> None:
    assert trace_url(raw) == expected


def test_trace_url_drops_third_party_idp_paths() -> None:
    """The reason this is stricter than ``_safe_url``.

    ``_safe_url`` redacts the path only for a small Google-OAuth host
    allowlist, keeping it everywhere else. A Workspace tenant can federate to
    any identity provider, so a one-time SAML assertion can sit in the path of
    a host no allowlist anticipates — and `-vv` output is what our issue
    template asks users to paste into public bug reports.
    """
    out = trace_url("https://idp.example.test/sso/saml/SECRET_ASSERTION?RelayState=SECRET_STATE")

    assert out == "https://idp.example.test/"
    assert "SECRET_ASSERTION" not in out
    assert "SECRET_STATE" not in out


# ---------------------------------------------------------------------------
# safe_page_url: the credential-safe, never-raising page.url reader
# ---------------------------------------------------------------------------


def test_safe_page_url_strips_credentials() -> None:
    page = MagicMock()
    page.url = "https://accounts.google.com/signin?continue=SECRET&f.sid=SECRET2"

    assert safe_page_url(page) == "https://accounts.google.com/"


def test_safe_page_url_strips_the_path_on_the_product_host_too() -> None:
    """No host is exempt — a notebook id is not worth the leak surface."""
    page = MagicMock()
    page.url = "https://notebook.google.com/notebook/abc#access_token=SECRET"

    assert safe_page_url(page) == "https://notebook.google.com/"


def test_safe_page_url_degrades_when_the_page_is_gone(
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    """A dead page must yield a placeholder, never an exception.

    Both call sites sit on the login path — the pre-wait line runs outside the
    ``PlaywrightError`` handler, so a raising ``page.url`` here would surface as
    a bare traceback instead of the browser-closed help text.
    """
    page = MagicMock()
    type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("target closed")))

    assert safe_page_url(page) == "<unavailable>"


# ---------------------------------------------------------------------------
# log_observed_navigations: inert when DEBUG is off
# ---------------------------------------------------------------------------


def test_no_listener_attached_when_debug_disabled(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="notebooklm")
    page = _FakePage()

    with log_observed_navigations(page):
        pass

    assert page.listeners == []
    assert page.removed == []


# ---------------------------------------------------------------------------
# log_observed_navigations: what it logs
# ---------------------------------------------------------------------------


def test_logs_each_main_frame_navigation(debug_logs: pytest.LogCaptureFixture) -> None:
    page = _FakePage()

    with log_observed_navigations(page):
        page.main_frame.url = "https://accounts.google.com/"
        page.navigate(page.main_frame)
        page.main_frame.url = "https://notebook.google.com/"
        page.navigate(page.main_frame)

    messages = [r.getMessage() for r in debug_logs.records if r.name == TRACE_LOGGER]
    assert messages == [
        "Login wait: navigated to https://accounts.google.com/",
        "Login wait: navigated to https://notebook.google.com/",
    ]


def test_ignores_sub_frame_navigations(debug_logs: pytest.LogCaptureFixture) -> None:
    """SSO iframes are noise — only the main frame decides the wait."""
    page = _FakePage()

    with log_observed_navigations(page):
        page.navigate(_frame("https://tracker.example/pixel"))

    assert [r for r in debug_logs.records if r.name == TRACE_LOGGER] == []


def test_logs_a_top_frame_that_is_not_the_cached_main_frame(
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    """A parent-less frame is the main frame even if identity does not match.

    Guards the failure mode that would silently re-break the fix: if Playwright
    ever hands the listener a different wrapper for the same frame, an
    identity-only filter would drop *every* navigation and restore the silence.
    """
    page = _FakePage(main_frame=object())
    top = _frame("https://notebook.google.com/")
    top.parent_frame = None

    with log_observed_navigations(page):
        page.navigate(top)

    assert [r.getMessage() for r in debug_logs.records if r.name == TRACE_LOGGER] == [
        "Login wait: navigated to https://notebook.google.com/"
    ]


def test_a_raising_main_frame_read_never_pre_empts_the_wait(
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    """``page.main_frame`` raising must not skip the block it wraps.

    ``getattr(..., None)`` only absorbs a *missing* attribute; a property that
    raises would propagate past the ``yield`` and cancel the wait outright.
    """
    page = MagicMock()
    type(page).main_frame = property(lambda self: (_ for _ in ()).throw(RuntimeError("dead page")))
    entered = False

    with log_observed_navigations(page):
        entered = True

    assert entered
    assert any("could not read the page's main frame" in r.getMessage() for r in debug_logs.records)


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        # Query string: the login continue-URL carries session material.
        ("https://accounts.google.com/signin?continue=SECRET_CONTINUE", "SECRET_CONTINUE"),
        # Fragment: OAuth implicit-flow access tokens.
        ("https://notebook.google.com/#access_token=SECRET_TOKEN", "SECRET_TOKEN"),
        # Userinfo: ``https://TOKEN@host/`` shapes.
        ("https://SECRET_USERINFO@notebooklm.google.com/", "SECRET_USERINFO"),
        # Path — but only on Google's OAuth hosts, where it is a grant code.
        ("https://accounts.google.com/o/oauth2/auth/SECRET_GRANT", "SECRET_GRANT"),
    ],
)
def test_navigation_urls_are_redacted(
    debug_logs: pytest.LogCaptureFixture, raw: str, must_not_contain: str
) -> None:
    page = _FakePage()

    with log_observed_navigations(page):
        page.main_frame.url = raw
        page.navigate(page.main_frame)

    records = [r for r in debug_logs.records if r.name == TRACE_LOGGER]
    assert len(records) == 1
    # Check the raw args too, not just the rendered message: a lazy %-arg that
    # still holds the secret would leak into any downstream handler.
    assert must_not_contain not in records[0].getMessage()
    assert must_not_contain not in repr(records[0].args)


def test_suppressed_failures_never_attach_a_traceback(
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    """No ``exc_info`` on the swallow paths — tracebacks bypass ``_safe_url``.

    Playwright embeds the offending URL in its exception messages
    (``net::ERR_ABORTED at https://…?f.sid=…``), so a rendered traceback would
    route a raw credential-bearing URL around this module's structural
    redaction and leave only the package's heuristic scrubber, which has no
    marker to match an opaque OAuth grant sitting in a URL *path*.
    """
    boom = "https://accounts.google.com/o/oauth2/auth/SECRET_GRANT_IN_MESSAGE"
    exploding = MagicMock()
    type(exploding).url = property(lambda self: (_ for _ in ()).throw(RuntimeError(boom)))
    page = _FakePage(main_frame=exploding)

    with log_observed_navigations(page):
        page.navigate(exploding)

    records = [r for r in debug_logs.records if r.name == TRACE_LOGGER]
    assert records
    for record in records:
        assert record.exc_info is None
        assert "SECRET_GRANT_IN_MESSAGE" not in record.getMessage()
    # The exception TYPE still survives — that is the whole diagnostic signal.
    assert any("(RuntimeError)" in r.getMessage() for r in records)


# ---------------------------------------------------------------------------
# log_observed_navigations: cannot destabilise the wait
# ---------------------------------------------------------------------------


def test_listener_is_detached_on_normal_exit(debug_logs: pytest.LogCaptureFixture) -> None:
    page = _FakePage()

    with log_observed_navigations(page):
        pass

    assert len(page.listeners) == 1
    assert page.removed == page.listeners


def test_listener_is_detached_when_the_block_raises(debug_logs: pytest.LogCaptureFixture) -> None:
    page = _FakePage()

    with pytest.raises(RuntimeError, match="boom"), log_observed_navigations(page):
        raise RuntimeError("boom")

    assert page.removed == page.listeners


def test_a_raising_listener_never_escapes(debug_logs: pytest.LogCaptureFixture) -> None:
    """A broken URL read must not surface as a login failure."""
    exploding = MagicMock()
    type(exploding).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("no url")))
    page = _FakePage(main_frame=exploding)

    with log_observed_navigations(page):
        page.navigate(exploding)  # must not raise

    assert any("could not read a navigation URL" in r.getMessage() for r in debug_logs.records)


def test_page_without_event_support_degrades_to_a_no_op(
    debug_logs: pytest.LogCaptureFixture,
) -> None:
    page = MagicMock()
    page.on.side_effect = AttributeError("no events on this build")
    entered = False

    with log_observed_navigations(page):
        entered = True

    assert entered
    assert any("navigation logging unavailable" in r.getMessage() for r in debug_logs.records)


def test_a_partial_attach_is_still_detached(debug_logs: pytest.LogCaptureFixture) -> None:
    """``page.on`` raising does not prove the handler was never registered.

    Detach is therefore unconditional — a listener left on a page the caller
    keeps driving would fire for the rest of the session.
    """
    page = MagicMock()
    page.on.side_effect = RuntimeError("raised after registering")

    with log_observed_navigations(page):
        pass

    page.remove_listener.assert_called_once()


def test_detach_failure_never_escapes(debug_logs: pytest.LogCaptureFixture) -> None:
    page = MagicMock()
    page.remove_listener.side_effect = RuntimeError("page already closed")

    with log_observed_navigations(page):
        pass

    assert any("could not detach" in r.getMessage() for r in debug_logs.records)


# ---------------------------------------------------------------------------
# Wired into the interactive login wait
# ---------------------------------------------------------------------------


class _CaptureIO:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emitted.append(args)

    def fail(self, code: int) -> Any:  # pragma: no cover - not reached
        raise AssertionError(f"unexpected io.fail({code})")

    def run_async(self, coro: Any) -> Any:  # pragma: no cover - not reached
        raise AssertionError("run_async not used by the neutral core")


class _FakeSyncPlaywright:
    def __init__(self, playwright: Any) -> None:
        self._playwright = playwright

    def __enter__(self) -> Any:
        return self._playwright

    def __exit__(self, *exc: Any) -> bool:
        return False


def _run_fake_interactive_login(tmp_path: Path) -> Any:
    """Drive ``run_browser_capture``'s interactive arm over a faked Playwright.

    The page starts off-host (so the human-wait branch is taken) and lands on
    the rebranded alias when ``wait_for_url`` is called, replaying a navigation
    through whatever listener the tracing installed. Returns the page mock so
    the caller can assert on listener bookkeeping.
    """
    profile = tmp_path / "browser_profile"
    profile.mkdir()

    page = MagicMock()
    page.url = "https://accounts.google.com/signin?continue=SECRET_CONTINUE"
    page.content.return_value = "<html></html>"
    observed: list[Any] = []
    page.on.side_effect = lambda event, handler: observed.append((event, handler))

    def _land(*_args: Any, **_kwargs: Any) -> None:
        for event, handler in observed:
            if event == "framenavigated":
                frame = page.main_frame
                frame.url = "https://notebook.google.com/"
                handler(frame)
        page.url = "https://notebook.google.com/"

    page.wait_for_url.side_effect = _land

    context = MagicMock()
    context.pages = [page]
    context.storage_state.return_value = {
        "cookies": [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
        "origins": [],
    }
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = context

    with patch(
        "playwright.sync_api.sync_playwright",
        side_effect=lambda: _FakeSyncPlaywright(playwright),
    ):
        run_browser_capture(
            BrowserCapturePlan(
                browser="chromium",
                browser_profile=profile,
                storage_path=tmp_path / "storage_state.json",
            ),
            _CaptureIO(),
            headless=False,
            interactive=True,
        )
    return page


@pytest.mark.requires_playwright
def test_interactive_wait_logs_accepted_hosts_and_navigations(
    tmp_path: Path, debug_logs: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end shape a stuck ``-vv login`` paste would have shown."""
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    page = _run_fake_interactive_login(tmp_path)

    messages = [
        r.getMessage() for r in debug_logs.records if r.name in (TRACE_LOGGER, CAPTURE_LOGGER)
    ]
    accept_lines = [m for m in messages if m.startswith("Login wait: accepting")]
    # Asserted verbatim: this line names every host that would end the wait,
    # and its absence is what made the notebook.google.com rebrand invisible.
    # The starting URL keeps its host and loses everything else, including the
    # ``continue=`` secret.
    assert accept_lines == [
        "Login wait: accepting any of notebook.google.com, notebooklm.google.com "
        "(currently on https://accounts.google.com/); timeout 300s"
    ]
    assert not any("SECRET_CONTINUE" in m for m in messages)
    # Plus the navigation that ended the wait.
    assert "Login wait: navigated to https://notebook.google.com/" in messages
    # Listener bookkeeping is balanced — nothing left attached to the page.
    page.remove_listener.assert_called_once()


@pytest.mark.requires_playwright
def test_interactive_wait_is_untouched_when_debug_is_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole feature must be invisible at the default log level.

    This is the constraint that makes the tracing safe to add to a five-minute
    interactive wait at all: with DEBUG off, no listener is registered on the
    page and not a single record is produced.
    """
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    caplog.set_level(logging.INFO, logger="notebooklm")

    page = _run_fake_interactive_login(tmp_path)

    page.on.assert_not_called()
    page.remove_listener.assert_not_called()
    assert [r for r in caplog.records if r.getMessage().startswith("Login wait:")] == []
