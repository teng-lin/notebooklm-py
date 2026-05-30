"""Unit tests for ``_enumerate_one_jar`` failure-outcome branches.

``_enumerate_one_jar`` probes one rookiepy cookie set against
``?authuser=N`` and returns either a list of :class:`Account` records or a
:class:`BrowserCookieOutcome` subclass on failure. The CLI-level fan-out
tests exercise the happy paths and the quiet network branch; these direct
unit tests pin the remaining validation-failure and stale-cookie outcome
shapes (both quiet and loud).
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from notebooklm.cli.services.login import cookie_jar
from notebooklm.cli.services.login.outcomes import (
    CookieValidationFailure,
    NetworkFailure,
    StaleCookies,
)


class _FakeAccount:
    def __init__(self, authuser, email, is_default):
        self.authuser = authuser
        self.email = email
        self.is_default = is_default


@contextmanager
def _enumerate_env(*, validate_return, run_async_side_effect=None, run_async_return=None):
    """Patch the collaborators ``_enumerate_one_jar`` reaches at call time.

    ``validate_with_recovery`` and ``run_async`` are module-level imports in
    ``cookie_jar`` (patched via ``patch.object``); ``build_cookie_jar`` /
    ``extract_cookies_with_domains`` / ``enumerate_accounts`` are *function-local*
    ``from ....auth import ...`` lookups, so they're patched on ``notebooklm.auth``.
    ``enumerate_accounts`` is async, so an auto-specced ``patch`` would hand back
    an un-awaited coroutine (``run_async`` is stubbed and never awaits it); an
    explicit sync ``MagicMock`` returns a plain sentinel instead.
    """
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(cookie_jar, "validate_with_recovery", return_value=validate_return)
        )
        stack.enter_context(patch("notebooklm.auth.extract_cookies_with_domains", return_value={}))
        stack.enter_context(patch("notebooklm.auth.build_cookie_jar", return_value=object()))
        stack.enter_context(
            patch("notebooklm.auth.enumerate_accounts", MagicMock(return_value=object()))
        )
        if run_async_side_effect is not None:
            stack.enter_context(
                patch.object(cookie_jar, "run_async", side_effect=run_async_side_effect)
            )
        else:
            stack.enter_context(
                patch.object(cookie_jar, "run_async", return_value=run_async_return)
            )
        yield


class TestValidationFailure:
    def test_quiet_returns_collapsed_validation_failure(self):
        with _enumerate_env(validate_return=({"cookies": []}, ValueError("missing SID"))):
            out = cookie_jar._enumerate_one_jar([], "chrome", None, quiet=True)

        assert isinstance(out, CookieValidationFailure)
        assert out.code == "COOKIE_VALIDATION_FAILED"
        # Quiet mode collapses to the single-line message naming the browser.
        assert "chrome" in out.message
        assert "\n" not in out.message

    def test_loud_returns_validation_failure_with_hint(self):
        with _enumerate_env(validate_return=({"cookies": []}, ValueError("missing SID"))):
            out = cookie_jar._enumerate_one_jar([], "chrome", None, quiet=False)

        assert isinstance(out, CookieValidationFailure)
        assert out.code == "COOKIE_VALIDATION_FAILED"
        # Loud mode includes the underlying error and a multi-line hint body.
        assert "missing SID" in out.message
        assert "No valid Google authentication cookies" in out.message


class TestStaleCookies:
    def test_quiet_returns_collapsed_stale_outcome(self):
        with _enumerate_env(
            validate_return=({"cookies": [1]}, None),
            run_async_side_effect=ValueError("rejected"),
        ):
            out = cookie_jar._enumerate_one_jar([{"x": 1}], "firefox", None, quiet=True)

        assert isinstance(out, StaleCookies)
        assert out.code == "STALE_COOKIES"
        assert "firefox" in out.message
        assert "too stale" in out.message

    def test_loud_returns_detailed_stale_outcome(self):
        with _enumerate_env(
            validate_return=({"cookies": [1]}, None),
            run_async_side_effect=ValueError("rejected"),
        ):
            out = cookie_jar._enumerate_one_jar([{"x": 1}], "firefox", None, quiet=False)

        assert isinstance(out, StaleCookies)
        assert out.code == "STALE_COOKIES"
        assert "Account discovery failed" in out.message
        assert "notebooklm login" in out.message


class TestNetworkFailure:
    def test_quiet_reraises_network_error(self):
        with (
            _enumerate_env(
                validate_return=({"cookies": [1]}, None),
                run_async_side_effect=httpx.ConnectError("no route"),
            ),
            pytest.raises(httpx.RequestError),
        ):
            cookie_jar._enumerate_one_jar([{"x": 1}], "chrome", None, quiet=True)

    def test_loud_returns_network_failure_outcome(self):
        with _enumerate_env(
            validate_return=({"cookies": [1]}, None),
            run_async_side_effect=httpx.ConnectError("no route"),
        ):
            out = cookie_jar._enumerate_one_jar([{"x": 1}], "chrome", None, quiet=False)

        assert isinstance(out, NetworkFailure)
        assert out.code == "NETWORK_ERROR"


class TestSuccessPath:
    def test_legacy_single_jar_returns_accounts_unchanged(self):
        accounts = [_FakeAccount(0, "a@gmail.com", True)]
        with _enumerate_env(validate_return=({"cookies": [1]}, None), run_async_return=accounts):
            out = cookie_jar._enumerate_one_jar([{"x": 1}], "chrome", None)

        assert out == accounts

    def test_fanout_tags_accounts_with_browser_profile(self):
        # Account is reconstructed inside the function from notebooklm.auth, so
        # leave the real class in place here (only the probe is stubbed).
        accounts = [_FakeAccount(0, "a@gmail.com", True)]
        with _enumerate_env(validate_return=({"cookies": [1]}, None), run_async_return=accounts):
            out = cookie_jar._enumerate_one_jar([{"x": 1}], "chrome", "Profile 1")

        assert len(out) == 1
        assert out[0].browser_profile == "Profile 1"
        assert out[0].email == "a@gmail.com"
