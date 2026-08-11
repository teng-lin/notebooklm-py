"""User-facing guidance must point at the host the client is actually using.

Every "open NotebookLM in your browser" instruction is really an instruction to
mint the per-product binding cookie (``OSID`` / ``__Secure-OSID``). Those are
host-scoped, and Google now serves the personal app from two hosts and redirects
between them — so the browser visit can succeed while the *configured* host is
left without a binding. Re-wording the URL does not fix that (the redirect
happens either way); naming the outcome does.

These tests pin the three properties that make the guidance safe:

1. The URL a message tells the user to open is derived from the configured host,
   never a fixed one — otherwise the user verifies a host the client never
   called.
2. The binding-related hints carry the cross-host scope caveat, naming both the
   host the cookies land on and the host the client uses, plus a recovery.
3. The caveat is silent where it does not apply — the enterprise host has no
   alias, ``__Secure-1PSIDTS`` is a ``.google.com`` cookie both hosts receive,
   and the experimental-host flag rides on the *rebrand* host rather than on
   "whichever host is not the configured one" (which would flag the legacy
   default for rebrand-host users).
"""

from __future__ import annotations

import pytest

from notebooklm._auth.cookie_policy import app_host_scope_note, missing_cookies_hint
from notebooklm._auth.extraction import _unavailable_redirect_message
from notebooklm._env import ENTERPRISE_BASE_HOST, PERSONAL_BASE_HOST, PERSONAL_LEGACY_HOST

# Cookie sets that steer ``missing_cookies_hint`` into each branch.
_BINDING_MISSING = {"SID", "__Secure-1PSIDTS"}
# LSID included so the secondary binding is *complete* (#1977) and this set
# reaches the psidts-only branch rather than the "no binding either" one.
_PSIDTS_MISSING = {"SID", "APISID", "SAPISID", "LSID"}
_EVERYTHING_MISSING = {"SID"}


@pytest.fixture
def select_host(monkeypatch):
    """Return a callable that selects a base host for the duration of a test."""

    def _select(host: str) -> None:
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{host}")

    return _select


class TestAppHostScopeNote:
    """The shared caveat behind every binding-related hint."""

    def test_names_both_personal_hosts_and_a_recovery(self, select_host):
        select_host(PERSONAL_BASE_HOST)

        note = app_host_scope_note()

        # The host the cookies land on, and the host the client is using.
        assert PERSONAL_LEGACY_HOST in note
        assert PERSONAL_BASE_HOST in note
        # Concrete recovery: re-login (mints the .google.com binding both hosts
        # accept) or select the host that holds the cookies.
        assert "notebooklm login" in note
        assert f"NOTEBOOKLM_BASE_URL=https://{PERSONAL_LEGACY_HOST}" in note

    def test_host_roles_follow_the_selection(self, select_host):
        """Selecting the alias inverts which host is 'the other one'."""
        select_host(PERSONAL_LEGACY_HOST)

        note = app_host_scope_note()

        assert f"NOTEBOOKLM_BASE_URL=https://{PERSONAL_BASE_HOST}" in note
        assert f"NOTEBOOKLM_BASE_URL=https://{PERSONAL_LEGACY_HOST}" not in note

    @pytest.mark.parametrize("selected", [PERSONAL_BASE_HOST, PERSONAL_LEGACY_HOST])
    def test_no_editorial_about_either_host(self, select_host, selected):
        """#2067 retired the "experimental" caveat instead of inverting it.

        It used to mark the rebrand host as "experimental -- not the documented
        default", asymmetrically so the words could never land on the legacy
        host and warn users off the fallback that actually worked. The flip made
        the rebrand host the documented default and moved the cassettes onto it,
        so every clause became false for it; re-pointing the same words at the
        legacy host would be false the other way, since that host is
        long-exercised and is now the documented rollback lever.

        Parametrized over both selections precisely because the old bug was an
        asymmetry: a caveat that reappears in only one direction still passes a
        single-host test.
        """
        select_host(selected)

        note = app_host_scope_note()

        assert "experimental" not in note
        assert "not the documented default" not in note

    def test_enterprise_host_has_no_sibling_and_no_note(self, select_host):
        select_host(ENTERPRISE_BASE_HOST)

        assert app_host_scope_note() == ""


class TestMissingCookiesHintNamesConfiguredHost:
    """``missing_cookies_hint`` derives its URL from the configured host."""

    @pytest.mark.parametrize(
        "cookie_names",
        [_BINDING_MISSING, _PSIDTS_MISSING, _EVERYTHING_MISSING],
        ids=["binding-missing", "psidts-missing", "both-missing"],
    )
    def test_open_url_tracks_the_selected_host(self, select_host, cookie_names):
        select_host(PERSONAL_LEGACY_HOST)

        hint = missing_cookies_hint(cookie_names, browser_label="chrome")

        assert f"https://{PERSONAL_LEGACY_HOST} in chrome" in hint

    @pytest.mark.parametrize(
        "cookie_names",
        [_BINDING_MISSING, _EVERYTHING_MISSING],
        ids=["binding-missing", "both-missing"],
    )
    def test_binding_branches_carry_the_scope_note(self, select_host, cookie_names):
        select_host(PERSONAL_BASE_HOST)

        hint = missing_cookies_hint(cookie_names, browser_label="chrome")

        assert app_host_scope_note() in hint

    def test_psidts_branch_omits_the_scope_note(self, select_host):
        """``__Secure-1PSIDTS`` is a ``.google.com`` cookie — no host scope to warn about."""
        select_host(PERSONAL_BASE_HOST)

        hint = missing_cookies_hint(_PSIDTS_MISSING, browser_label="chrome")

        assert "host-scoped" not in hint

    def test_enterprise_selection_keeps_hints_free_of_personal_hosts(self, select_host):
        select_host(ENTERPRISE_BASE_HOST)

        hint = missing_cookies_hint(_BINDING_MISSING, browser_label="chrome")

        assert f"https://{ENTERPRISE_BASE_HOST} in chrome" in hint
        assert PERSONAL_BASE_HOST not in hint
        assert PERSONAL_LEGACY_HOST not in hint


class TestUnavailableRedirectMessage:
    """The region-gate message must ask the user to reproduce on the right host."""

    def test_verification_url_tracks_the_selected_host(self, select_host):
        select_host(PERSONAL_LEGACY_HOST)

        message = _unavailable_redirect_message(
            "https://notebooklm.google/unavailable?location=unsupported"
        )

        assert f"https://{PERSONAL_LEGACY_HOST} in a normal browser" in message
        # The gate diagnostic itself is preserved.
        assert "location=unsupported" in message
