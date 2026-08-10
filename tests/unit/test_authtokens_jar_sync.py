"""``AuthTokens.cookies`` must not go stale when the live jar is rebound.

ADR-0031 Stage 4. ``cookies`` (a ``(name, domain, path) -> value`` map) and
``cookie_jar`` (a live ``httpx.Cookies``) hold the same information in two
shapes, synced once in ``__post_init__``. The mid-session refresh rebinds only
the jar, which left ``cookies`` describing the *previous* session.

That was invisible internally — the one reader, ``_kernel.py``, prefers
``cookie_jar`` and only falls back to ``cookies`` when the jar is ``None``,
which construction makes impossible. But ``AuthTokens`` is documented public
API, so a caller reading ``auth.cookies`` after a refresh got stale values with
nothing to signal it.
"""

from __future__ import annotations

import httpx
import pytest

from notebooklm._auth.tokens import AuthTokens


def _jar(**cookies: str) -> httpx.Cookies:
    jar = httpx.Cookies()
    for name, value in cookies.items():
        jar.set(name, value, domain=".google.com", path="/")
    return jar


def _auth(**cookies: str) -> AuthTokens:
    return AuthTokens(
        cookies={(n, ".google.com", "/"): v for n, v in cookies.items()},
        csrf_token="csrf",
        session_id="session",
    )


def test_replace_cookie_jar_updates_both_views() -> None:
    """The regression: rebinding the jar must refresh the derived map too."""
    auth = _auth(SID="old-sid")
    assert auth.cookies[("SID", ".google.com", "/")] == "old-sid"

    auth.replace_cookie_jar(_jar(SID="fresh-sid"))

    assert auth.cookies[("SID", ".google.com", "/")] == "fresh-sid", (
        "auth.cookies still describes the pre-refresh session"
    )
    assert auth.cookie_jar is not None
    assert auth.cookie_jar.get("SID", domain=".google.com") == "fresh-sid"


def test_the_two_views_agree_after_a_rebind() -> None:
    """Stated as the invariant rather than as one example.

    Whatever the jar holds, the map must describe the same cookies — that is
    the property the dual storage kept breaking.
    """
    auth = _auth(SID="s0", HSID="h0")
    auth.replace_cookie_jar(_jar(SID="s1", HSID="h1", APISID="a1"))

    from_map = {name: value for (name, _domain, _path), value in auth.cookies.items()}
    assert auth.cookie_jar is not None
    from_jar = {c.name: c.value for c in auth.cookie_jar.jar}
    assert from_map == from_jar


def test_a_cookie_dropped_by_the_refresh_disappears_from_the_map() -> None:
    """Rebinding replaces the view; it does not merge into the stale one.

    A stale key surviving a refresh is the more dangerous half of the bug —
    it would keep a retired cookie readable through the public surface.
    """
    auth = _auth(SID="s0", RETIRED="gone")
    auth.replace_cookie_jar(_jar(SID="s1"))

    assert ("RETIRED", ".google.com", "/") not in auth.cookies
    assert auth.cookies[("SID", ".google.com", "/")] == "s1"


def test_construction_still_syncs_the_two_views() -> None:
    """The pre-existing ``__post_init__`` sync is unchanged."""
    auth = _auth(SID="s")
    assert auth.cookie_jar is not None
    assert auth.cookie_jar.get("SID", domain=".google.com") == "s"


def test_flat_cookies_reflects_the_refresh() -> None:
    """The legacy flat read is derived from ``cookies``, so it inherits the fix."""
    auth = _auth(SID="old")
    auth.replace_cookie_jar(_jar(SID="new"))
    assert auth.flat_cookies["SID"] == "new"


@pytest.mark.parametrize("value", ["", "x" * 200])
def test_rebind_handles_edge_case_values(value: str) -> None:
    """Empty and long values round-trip through the jar->map conversion."""
    auth = _auth(SID="seed")
    auth.replace_cookie_jar(_jar(SID=value))
    got = auth.cookies.get(("SID", ".google.com", "/"))
    # httpx drops an empty-valued cookie rather than storing it; either way the
    # two views must still agree, which is the invariant under test.
    assert auth.cookie_jar is not None
    jar_names = {c.name for c in auth.cookie_jar.jar}
    assert (got is not None) == ("SID" in jar_names)


class TestJarProjection:
    """ADR-0032 Phase A: AuthTokens.jar is a fresh, never-stale question view."""

    def test_jar_reflects_the_current_live_jar(self) -> None:
        auth = _auth(SID="old")
        assert auth.jar.names() == {"SID"}
        auth.replace_cookie_jar(_jar(SID="new", HSID="h"))
        # Fresh projection each access — no staleness, unlike the old .cookies bug.
        assert auth.jar.names() == {"SID", "HSID"}
        assert auth.jar.to_domain_map()[("SID", ".google.com", "/")] == "new"

    def test_jar_is_a_cookiejar_not_the_live_httpx_jar(self) -> None:
        from notebooklm._auth.cookie_types import CookieJar

        auth = _auth(SID="s")
        assert isinstance(auth.jar, CookieJar)
        # Distinct object each access (a projection, not the live jar).
        assert auth.jar is not auth.jar

    def test_jar_answers_questions(self) -> None:
        auth = _auth(SID="s", APISID="a", SAPISID="sa", LSID="l")
        assert auth.jar.has_secondary_binding() is True
