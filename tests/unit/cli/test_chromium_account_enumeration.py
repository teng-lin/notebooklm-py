"""Failure-path, dedupe, and non-leak contracts for the Chromium account fan-out.

``cli/services/login/chromium_accounts.py`` is the only login-service module
that holds *two* cookie jars at once (the per-profile ``raw`` rows plus the
aggregated ``per_profile_cookies`` map) while building user-facing text. The
existing suite covers its happy paths through the CLI (``test_login_chromium_
fanout.py``); what was untested is everything the module does when a profile
cannot be read, yields no accounts, or duplicates an email already seen — and
whether any of those messages can carry a cookie value out with them.

Deliberate testing choices:

* The ``browser::profile`` spec splitter is driven with real strings against
  the real ``_chromium_profiles`` predicate — it is pure, so there is nothing
  to fake.
* The fan-out and scoped-read tests patch only *public* attributes of the real
  ``notebooklm.cli._chromium_profiles`` / ``notebooklm.auth`` modules (the
  disk-read and network-probe boundaries), the same seam
  ``_session_helpers._install_chromium_fanout_patches`` uses. Everything
  between those boundaries — ``_enumerate_one_jar``, the real
  ``validate_with_recovery`` preflight, ``project_browser_account`` — runs for
  real, so a regression anywhere in that chain reaches these assertions.
* Cookie rows without ``SID`` fail validation with no network at all
  (``recover_psidts_in_memory`` short-circuits on a missing ``SID`` before it
  would attempt a ``RotateCookies`` rotation), which is what lets the
  "signed-out profile" tests drive the real code instead of a stub outcome.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import notebooklm.auth as auth_module
import notebooklm.cli._chromium_profiles as chromium_profiles
from notebooklm.auth import Account
from notebooklm.cli._chromium_profiles import ChromiumProfile
from notebooklm.cli.services.login import chromium_accounts
from notebooklm.cli.services.login.outcomes import (
    BrowserCookieOutcome,
    CookieValidationFailure,
)
from tests._fixtures.login_io import RecordingLoginIO

# Obviously-fake sentinel embedded in every synthetic cookie value. The
# non-leak assertions below prove it never reaches a rendered outcome message
# or an emitted progress line.
_FAKE_COOKIE_SENTINEL = "fake-cookie-value"


def _cookie_row(name: str, value: str) -> dict[str, Any]:
    return {
        "domain": ".google.com",
        "name": name,
        "value": value,
        "path": "/",
        "secure": True,
        "expires": 4102444800,
        "http_only": False,
    }


def _signed_in_rows(directory_name: str) -> list[dict[str, Any]]:
    """Rows that clear the network-free validation preflight.

    ``SID`` plus a far-future ``.google.com`` ``__Secure-1PSIDTS`` is the
    minimum ``validate_with_recovery`` accepts without attempting a heal. The
    directory name is folded into the ``SID`` value so the stubbed probe can
    tell which profile's jar it was handed.
    """
    return [
        _cookie_row("SID", f"{_FAKE_COOKIE_SENTINEL}-{directory_name}"),
        _cookie_row("__Secure-1PSIDTS", f"{_FAKE_COOKIE_SENTINEL}-psidts-{directory_name}"),
    ]


def _signed_out_rows() -> list[dict[str, Any]]:
    """Rows with no ``SID`` — the real validator rejects these offline."""
    return [_cookie_row("NID", f"{_FAKE_COOKIE_SENTINEL}-nid")]


def _profile(tmp_path: Path, directory_name: str, human_name: str) -> ChromiumProfile:
    db = tmp_path / f"{directory_name}-Cookies"
    db.write_bytes(b"")  # presence-only; the reader is always stubbed
    return ChromiumProfile(
        browser="chrome",
        directory_name=directory_name,
        human_name=human_name,
        cookies_db=db,
    )


@contextlib.contextmanager
def _patched_boundaries(
    reader: Callable[..., list[dict[str, Any]]],
    accounts_by_directory: dict[str, list[Account]] | None = None,
    *,
    profiles: list[ChromiumProfile] | None = None,
) -> Iterator[list[str]]:
    """Patch only the disk-read / discovery / network-probe boundaries.

    Yields the ordered list of profile directory names the reader was asked
    for, so tests can assert on short-circuit behaviour.

    ``accounts_by_directory`` maps a profile directory name to the accounts its
    jar should yield; a jar whose ``SID`` matches no entry fails the assertion
    rather than silently returning nothing.
    """
    read_calls: list[str] = []
    resolved = accounts_by_directory or {}

    def recording_reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        read_calls.append(profile.directory_name)
        return reader(profile, domains=domains)

    async def fake_enumerate(jar: Any, *args: Any, **kwargs: Any) -> list[Account]:
        sid = jar.get("SID", default="")
        for directory_name, accounts in resolved.items():
            if sid == f"{_FAKE_COOKIE_SENTINEL}-{directory_name}":
                return list(accounts)
        raise AssertionError(f"unexpected account probe for jar SID={sid!r}")

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                chromium_profiles,
                "read_chromium_profile_cookies",
                side_effect=recording_reader,
            )
        )
        stack.enter_context(
            patch.object(auth_module, "enumerate_accounts", side_effect=fake_enumerate)
        )
        if profiles is not None:
            stack.enter_context(
                patch.object(
                    chromium_profiles,
                    "discover_chromium_profiles",
                    return_value=list(profiles),
                )
            )
        yield read_calls


def _assert_no_cookie_leak(*texts: str) -> None:
    for text in texts:
        assert _FAKE_COOKIE_SENTINEL not in text, f"cookie value leaked into: {text!r}"


# ---------------------------------------------------------------------------
# ``browser::profile`` spec splitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param("chrome::Work", ("chrome", "Work"), id="human-name-selector"),
        pytest.param("chrome::Profile 1", ("chrome", "Profile 1"), id="directory-selector"),
        pytest.param("chrome::  Work  ", ("chrome", "Work"), id="selector-is-stripped"),
        pytest.param("  chrome  ::Work", ("chrome", "Work"), id="browser-is-stripped"),
        pytest.param("chrome::", ("chrome", ""), id="empty-selector-still-splits"),
        pytest.param("chrome::a::b", ("chrome", "a::b"), id="only-first-separator-splits"),
        pytest.param("opera_gx::Work", ("opera_gx", "Work"), id="alias-spelling-preserved"),
    ],
)
def test_profile_spec_split_preserves_the_caller_spelling_of_the_browser(
    spec: str, expected: tuple[str, str]
) -> None:
    """The splitter hands the caller's own spelling back, uncanonicalised.

    ``opera_gx`` is recognised as Chromium-family only after the predicate
    normalises the underscore, but the returned browser must stay the spelling
    the user typed — downstream ``resolve_chromium_profile`` normalises again,
    and rewriting it here would silently change the browser in error text.
    """
    assert chromium_accounts._split_chromium_profile_browser_spec(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param("chrome", id="no-separator"),
        pytest.param("::Profile 1", id="empty-browser"),
        pytest.param("   ::Profile 1", id="whitespace-only-browser"),
        pytest.param("firefox::default", id="firefox-is-not-chromium-family"),
        pytest.param("safari::Work", id="safari-is-not-chromium-family"),
    ],
)
def test_profile_spec_split_declines_specs_it_cannot_own(spec: str) -> None:
    """Returning ``None`` is what routes the spec to the non-Chromium reader.

    An empty or non-Chromium base must fall through rather than be resolved as
    a profile selector: ``firefox::default`` is a Firefox container spec, and
    ``::Profile 1`` is a typo. Claiming either would send it to
    ``resolve_chromium_profile`` and surface a Chromium error for a browser
    that has no Chromium profile layout at all.
    """
    assert chromium_accounts._split_chromium_profile_browser_spec(spec) is None


# ---------------------------------------------------------------------------
# Scoped single-profile reads (``chrome::<selector>``)
# ---------------------------------------------------------------------------


def _read_scoped(io: RecordingLoginIO, selector: str, *, verbose: bool = False) -> Any:
    return chromium_accounts._read_chromium_profile_cookies_from_selector(
        io,
        "chrome",
        selector,
        verbose=verbose,
        include_domains=None,
    )


def test_scoped_read_reports_an_unresolvable_selector_with_the_available_choices(
    tmp_path: Path,
) -> None:
    """A typo'd selector must name what *is* available, not just fail.

    The message is the user's only clue about how their profiles are actually
    named on disk, so it has to carry both the human name and the directory.
    """
    profiles = [_profile(tmp_path, "Default", "Personal")]
    with _patched_boundaries(lambda *a, **k: [], profiles=profiles):
        result = _read_scoped(RecordingLoginIO(), "Nope")

    assert isinstance(result, CookieValidationFailure)
    assert result.code == "CHROMIUM_PROFILE_INVALID"
    assert "chrome profile 'Nope' was not found" in result.message
    assert "Personal (directory: Default)" in result.message


def test_scoped_read_reports_the_missing_rookie_cookies_dependency_with_an_install_hint(
    tmp_path: Path,
) -> None:
    """``ImportError`` means the optional extra is absent, not that cookies are bad.

    It gets its own code so the caller can render an install hint instead of a
    "your cookies are invalid" message that would send the user to re-login.
    """
    profiles = [_profile(tmp_path, "Profile 1", "Work")]

    def raise_import_error(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        raise ImportError("No module named 'rookie_cookies'")

    with _patched_boundaries(raise_import_error, profiles=profiles):
        result = _read_scoped(RecordingLoginIO(), "Work")

    assert isinstance(result, CookieValidationFailure)
    assert result.code == "ROOKIEPY_NOT_INSTALLED"
    assert "pip install 'notebooklm-py[cookies]'" in result.message


@pytest.mark.parametrize(
    ("error", "expected_fragment"),
    [
        pytest.param(
            OSError("database is locked"),
            "browser database is locked",
            id="locked-cookie-db",
        ),
        pytest.param(
            OSError("permission denied opening profile"),
            "Permission denied reading",
            id="permission-denied",
        ),
        pytest.param(
            RuntimeError("could not decrypt value"),
            "Could not decrypt",
            id="decryption-failure",
        ),
        pytest.param(
            RuntimeError("cookie store exploded"),
            "Failed to read cookies from",
            id="unclassified-failure",
        ),
    ],
)
def test_scoped_read_labels_read_failures_with_the_browser_and_profile(
    tmp_path: Path, error: Exception, expected_fragment: str
) -> None:
    """The friendly message must name *which* profile failed, not just the browser.

    A scoped read is requested precisely because the user has several
    profiles; ``chrome`` alone would not tell them which one to close or
    unlock. ``OSError`` is raised directly rather than as a platform-specific
    subclass — POSIX and Windows disagree on which subclass a locked SQLite
    file produces.
    """
    profiles = [_profile(tmp_path, "Profile 1", "Work")]

    def raise_read_error(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        raise error

    with _patched_boundaries(raise_read_error, profiles=profiles):
        result = _read_scoped(RecordingLoginIO(), "Work")

    assert isinstance(result, CookieValidationFailure)
    assert result.code == "COOKIE_READ_FAILED"
    assert expected_fragment in result.message
    assert "chrome profile 'Work'" in result.message


@pytest.mark.parametrize("verbose", [True, False], ids=["verbose", "quiet"])
def test_scoped_read_announces_the_resolved_profile_only_when_verbose(
    tmp_path: Path, verbose: bool
) -> None:
    """The progress line is the only place the resolved directory is shown.

    JSON-output callers pass ``verbose=False`` and must get a silent sink —
    a stray status line there would corrupt the envelope on stdout.
    """
    profiles = [_profile(tmp_path, "Profile 1", "Work")]
    rows = _signed_in_rows("Profile 1")
    io = RecordingLoginIO()

    with _patched_boundaries(lambda *a, **k: rows, profiles=profiles):
        result = _read_scoped(io, "Work", verbose=verbose)

    assert result == (profiles[0], rows)
    if verbose:
        assert io.emitted == [
            "[yellow]Reading cookies from chrome profile 'Work' (directory: Profile 1)...[/yellow]"
        ]
    else:
        assert io.emitted == []


# ---------------------------------------------------------------------------
# Multi-profile fan-out
# ---------------------------------------------------------------------------


def _fanout(io: RecordingLoginIO, profiles: list[ChromiumProfile], *, verbose: bool = False) -> Any:
    return chromium_accounts._enumerate_chromium_profiles_fanout(
        io,
        "chrome",
        profiles,
        verbose=verbose,
        include_domains=None,
    )


def test_fanout_aborts_on_a_missing_dependency_without_reading_later_profiles(
    tmp_path: Path,
) -> None:
    """A missing extra is global, so retrying every profile only wastes time.

    ``ImportError`` returns immediately rather than being collected as a
    per-profile read failure — the second profile must never be touched.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]

    def raise_import_error(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        raise ImportError("No module named 'rookie_cookies'")

    with _patched_boundaries(raise_import_error) as read_calls:
        result = _fanout(RecordingLoginIO(), profiles, verbose=True)

    assert isinstance(result, CookieValidationFailure)
    assert result.code == "ROOKIEPY_NOT_INSTALLED"
    assert "pip install 'notebooklm-py[cookies]'" in result.message
    assert read_calls == ["Default"]


@pytest.mark.parametrize("verbose", [True, False], ids=["verbose", "quiet"])
def test_fanout_skips_an_unreadable_profile_and_keeps_going(tmp_path: Path, verbose: bool) -> None:
    """One locked profile must not cost the user the profiles that do work.

    The skip note is verbose-only: under ``verbose=False`` the sink has to stay
    completely silent, since the caller may be emitting a JSON envelope.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]

    def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        if profile.directory_name == "Default":
            raise OSError("database is locked")
        return _signed_in_rows("Profile 1")

    accounts = {"Profile 1": [Account(0, "bob@example.com", True)]}
    io = RecordingLoginIO()
    with _patched_boundaries(reader, accounts) as read_calls:
        result = _fanout(io, profiles, verbose=verbose)

    assert read_calls == ["Default", "Profile 1"]
    per_profile_cookies, aggregated = result
    assert list(per_profile_cookies) == ["Profile 1"]
    assert [account.email for account in aggregated] == ["bob@example.com"]
    if verbose:
        assert io.emitted == [
            "[yellow]Reading cookies from 2 chrome user-profiles: 'Personal', 'Work'[/yellow]",
            "  [yellow]skipping chrome profile 'Personal': database is locked[/yellow]",
        ]
    else:
        assert io.emitted == []


def test_fanout_reports_the_first_read_error_when_no_profile_can_be_read(
    tmp_path: Path,
) -> None:
    """With zero successful reads the cause is a read failure, not a sign-out.

    Telling the user "no accounts found" here would send them to sign in again
    when the real fix is closing the browser, so this path reports the first
    underlying error and names the profile it came from.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]

    def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        raise OSError(f"database is locked ({profile.directory_name})")

    with _patched_boundaries(reader) as read_calls:
        result = _fanout(RecordingLoginIO(), profiles)

    assert read_calls == ["Default", "Profile 1"]
    assert isinstance(result, CookieValidationFailure)
    assert result.code == "COOKIE_READ_FAILED"
    assert "Could not read cookies from any chrome user-profile." in result.message
    assert "First error (Personal): database is locked (Default)" in result.message


@pytest.mark.parametrize("verbose", [True, False], ids=["verbose", "quiet"])
def test_fanout_reports_no_accounts_when_every_readable_profile_is_signed_out(
    tmp_path: Path, verbose: bool
) -> None:
    """Readable-but-signed-out is a different diagnosis from unreadable.

    Every profile's jar is read successfully and rejected by the real
    validation preflight, so the outcome must be ``NO_ACCOUNTS_FOUND`` (sign
    in) rather than ``COOKIE_READ_FAILED`` (close the browser). The account
    probe must never be reached — validation rejects these rows offline.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]

    with _patched_boundaries(lambda *a, **k: _signed_out_rows()) as read_calls:
        io = RecordingLoginIO()
        result = _fanout(io, profiles, verbose=verbose)

    assert read_calls == ["Default", "Profile 1"]
    assert isinstance(result, CookieValidationFailure)
    assert result.code == "NO_ACCOUNTS_FOUND"
    assert "No signed-in Google accounts found across 2 chrome user-profiles." in result.message
    skip_notes = [line for line in io.emitted if "no signed-in Google accounts in" in line]
    if verbose:
        assert skip_notes == [
            "  [dim]no signed-in Google accounts in 'Personal'[/dim]",
            "  [dim]no signed-in Google accounts in 'Work'[/dim]",
        ]
    else:
        assert io.emitted == []


def test_fanout_warns_once_and_keeps_the_first_profiles_cookies_for_a_shared_email(
    tmp_path: Path,
) -> None:
    """A duplicate email must resolve deterministically to the earlier profile.

    Profiles arrive ``Default`` first, so ``Default`` wins and the later
    profile is dropped with a warning. The warning deliberately names the
    losing profile by its *human* name and the winning one by its *directory*
    name — asserting both spellings keeps the message stable.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]
    accounts = {
        "Default": [Account(0, "dup@example.com", True)],
        "Profile 1": [Account(0, "dup@example.com", True), Account(1, "solo@example.com", False)],
    }

    def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        return _signed_in_rows(profile.directory_name)

    io = RecordingLoginIO()
    with _patched_boundaries(reader, accounts):
        per_profile_cookies, aggregated = _fanout(io, profiles, verbose=True)

    assert [account.email for account in aggregated] == ["dup@example.com", "solo@example.com"]
    assert [account.browser_profile for account in aggregated] == ["Default", "Profile 1"]
    # Compare EVERY warning line, not just the last: a duplicate emitted before
    # the final one would otherwise slip through, and one warning per collision
    # is the actual contract.
    assert [line for line in io.emitted if "warning:" in line] == [
        "  [yellow]warning: dup@example.com also appears in 'Work'; "
        "using cookies from 'Default'[/yellow]"
    ]
    # Both jars are retained: 'Profile 1' still contributed ``solo@example.com``.
    assert sorted(per_profile_cookies) == ["Default", "Profile 1"]


def test_fanout_marks_exactly_one_account_as_the_global_default(tmp_path: Path) -> None:
    """Each profile has its own default; the merged list may only have one.

    Both profiles report a locally-default account. Without the
    ``global_default_assigned`` latch the aggregate would carry two defaults
    and the caller would pick a profile arbitrarily.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]
    accounts = {
        "Default": [Account(0, "alice@example.com", True)],
        "Profile 1": [Account(0, "bob@example.com", True)],
    }

    def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
        return _signed_in_rows(profile.directory_name)

    with _patched_boundaries(reader, accounts):
        _, aggregated = _fanout(RecordingLoginIO(), profiles)

    assert [(account.email, account.is_default) for account in aggregated] == [
        ("alice@example.com", True),
        ("bob@example.com", False),
    ]


@pytest.mark.parametrize(
    "scenario",
    ["read-error", "signed-out", "duplicate-email"],
)
def test_fanout_never_puts_a_cookie_value_in_a_message_or_a_progress_line(
    tmp_path: Path, scenario: str
) -> None:
    """Nothing the fan-out renders may carry a cookie value out of the jar.

    The fan-out holds raw cookie rows in ``raw`` and ``per_profile_cookies``
    while formatting skip notes, dedupe warnings, and failure messages. Every
    synthetic cookie value here contains a single sentinel, so any future
    message that interpolates a jar instead of a profile label fails this test
    rather than shipping a credential into a console line or a JSON envelope.
    """
    profiles = [_profile(tmp_path, "Default", "Personal"), _profile(tmp_path, "Profile 1", "Work")]
    accounts: dict[str, list[Account]] = {}

    if scenario == "read-error":

        def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
            raise OSError("database is locked")

    elif scenario == "signed-out":

        def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
            return _signed_out_rows()

    else:

        def reader(profile: ChromiumProfile, *, domains: list[str]) -> Any:
            return _signed_in_rows(profile.directory_name)

        accounts = {
            "Default": [Account(0, "dup@example.com", True)],
            "Profile 1": [Account(0, "dup@example.com", True)],
        }

    io = RecordingLoginIO()
    with _patched_boundaries(reader, accounts):
        result = _fanout(io, profiles, verbose=True)

    _assert_no_cookie_leak(*io.emitted)
    if isinstance(result, BrowserCookieOutcome):
        _assert_no_cookie_leak(result.message)
