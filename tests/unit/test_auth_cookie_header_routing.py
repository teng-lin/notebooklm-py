"""Domain-correct cookie selection for raw ``Cookie:`` headers (issue #2054).

The flat ``name -> value`` projection behind :attr:`AuthTokens.cookie_header`
keeps one value per cookie name, so a name that legitimately exists on two hosts
loses all but one -- and *which* one survives is arbitrary, because
``_auth_domain_priority``'s named tiers are not all distinct (the two app hosts
share a tier, as do their dotted variants) and within a tier the first entry in
iteration order wins.

Every assertion here is on ``(name, value)`` pairs rather than names. A name-only
view cannot express the contract that matters -- "the *app-host* ``OSID`` never
reaches the sign-in host" -- once the sign-in host legitimately carries an
``OSID`` of its own, which is the ordinary post-rebrand state.
"""

from __future__ import annotations

import pytest

from notebooklm._auth.cookies import extract_cookies_from_storage
from notebooklm._auth.tokens import AuthTokens

# Named by ROLE as of #2067, and kept as literals on purpose: these tests
# assert concrete cookie routing, so deriving them from the constants under
# test would make them tautological. The previous names (APP_HOST for the
# legacy host, ALIAS_HOST for what is now the default) inverted after the flip
# — exactly the confusion that produced this branch's host-role bugs.
LEGACY_HOST = "notebooklm.google.com"
DEFAULT_HOST = "notebook.google.com"
SIGN_IN_HOST = "accounts.google.com"


def _cookie_pairs(header: str) -> set[tuple[str, str]]:
    """``(name, value)`` pairs from a ``Cookie:`` header."""
    pairs: set[tuple[str, str]] = set()
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            name, value = chunk.split("=", 1)
            pairs.add((name.strip(), value.strip()))
    return pairs


@pytest.fixture
def collided_auth() -> AuthTokens:
    """A session whose ``OSID`` exists on three hosts with three distinct values.

    This is the shape a real profile has after the rebrand: direct inspection of
    a live ``storage_state.json`` shows host-scoped ``OSID`` / ``__Secure-OSID``
    on **both** app hosts with **different values**, alongside the sign-in host's
    own. The collision is the whole point -- a fixture carrying one ``OSID``
    would pass every assertion below against a broken implementation.
    """
    return AuthTokens(
        cookies={
            ("SID", ".google.com", "/"): "v-sid",
            ("__Secure-1PSIDTS", ".google.com", "/"): "v-psidts",
            ("OSID", LEGACY_HOST, "/"): "osid-app",
            ("OSID", DEFAULT_HOST, "/"): "osid-alias",
            ("OSID", SIGN_IN_HOST, "/"): "osid-signin",
            ("LSID", SIGN_IN_HOST, "/"): "v-lsid",
        },
        csrf_token="csrf",
        session_id="session",
    )


@pytest.mark.parametrize(
    ("host", "expected_osid"),
    [(LEGACY_HOST, "osid-app"), (DEFAULT_HOST, "osid-alias"), (SIGN_IN_HOST, "osid-signin")],
)
def test_each_host_receives_its_own_binding_cookie(
    collided_auth: AuthTokens, host: str, expected_osid: str
) -> None:
    """Presence: the routed header carries the ``OSID`` minted for that host."""
    pairs = _cookie_pairs(collided_auth.cookie_header_for(f"https://{host}/x"))
    assert ("OSID", expected_osid) in pairs


@pytest.mark.parametrize(
    ("host", "forbidden"),
    [
        (LEGACY_HOST, {("OSID", "osid-alias"), ("OSID", "osid-signin"), ("LSID", "v-lsid")}),
        (DEFAULT_HOST, {("OSID", "osid-app"), ("OSID", "osid-signin"), ("LSID", "v-lsid")}),
        (SIGN_IN_HOST, {("OSID", "osid-app"), ("OSID", "osid-alias")}),
    ],
)
def test_no_host_receives_another_hosts_cookies(
    collided_auth: AuthTokens, host: str, forbidden: set[tuple[str, str]]
) -> None:
    """Absence: a cookie scoped to one host never reaches another.

    Presence alone is satisfiable by broadcasting everything to everyone, which
    is exactly the pre-fix behaviour -- so absence is the assertion that has
    teeth. ``LSID`` is the concrete leak #2054 reported: accounts-scoped, and
    sent to the app host on every probe by the flat projection.
    """
    pairs = _cookie_pairs(collided_auth.cookie_header_for(f"https://{host}/x"))
    assert not (pairs & forbidden)


def test_domain_wide_cookies_reach_every_host(collided_auth: AuthTokens) -> None:
    """Negative control: ``.google.com`` cookies are *supposed* to be broadcast.

    Without this, an implementation that sent nothing at all would satisfy every
    absence assertion above.
    """
    for host in (LEGACY_HOST, DEFAULT_HOST, SIGN_IN_HOST):
        pairs = _cookie_pairs(collided_auth.cookie_header_for(f"https://{host}/x"))
        assert ("SID", "v-sid") in pairs
        assert ("__Secure-1PSIDTS", "v-psidts") in pairs


def test_flat_projection_leaks_across_hosts(collided_auth: AuthTokens) -> None:
    """Characterize the projection this method replaces (passes on ``origin/main``).

    Not a regression test -- it pins the documented behaviour of
    :attr:`AuthTokens.cookie_header` so that changing it without updating the
    docstring fails here. The leak is real: the accounts-scoped ``LSID`` appears
    in a header a caller would send to the app host, and exactly one of the three
    ``OSID`` values survives.
    """
    pairs = _cookie_pairs(collided_auth.cookie_header)
    assert ("LSID", "v-lsid") in pairs, "flat projection is domain-blind by construction"
    surviving = {value for name, value in pairs if name == "OSID"}
    assert len(surviving) == 1, "one slot per name means two OSID values are discarded"


def test_non_https_url_is_rejected(collided_auth: AuthTokens) -> None:
    """An ``http`` URL must raise, not silently return some other header.

    Selection is ``Secure``-aware, and what a jar carries depends on how it was
    built: ``build_cookie_jar`` marks nothing ``Secure`` when widening a bare
    cookie map, while ``build_httpx_cookies_from_storage`` preserves the flag
    from disk. So an ``http`` URL returns anything from the full header to a
    partial one to none at all -- no single emptiness assertion covers it, and
    every outcome is wrong for auth. Rejecting the scheme is the contract.
    """
    with pytest.raises(ValueError, match="https"):
        collided_auth.cookie_header_for(f"http://{LEGACY_HOST}/x")


#: Tier-1 cookies every storage fixture needs, or the loader rejects it outright.
_REQUIRED = [
    {"name": "SID", "value": "v-sid", "domain": ".google.com", "path": "/"},
    {"name": "__Secure-1PSIDTS", "value": "v-psidts", "domain": ".google.com", "path": "/"},
]


def _storage(cookies: list[dict[str, object]]) -> dict[str, object]:
    return {"cookies": [*_REQUIRED, *cookies], "origins": []}


def test_same_tier_resolution_is_order_dependent() -> None:
    """The tie is real and observable -- two orderings, two answers.

    ``.notebooklm.google.com`` and ``.notebook.google.com`` share a tier, so the
    winner is whichever appears first. Deliberately two fixed orderings rather
    than a shuffle: a randomized gate would detect a real collision only ~50% of
    the time and the first green re-run would be believed.
    """
    entries = [
        {"name": "OSID", "value": "from-legacy", "domain": LEGACY_HOST, "path": "/"},
        {"name": "OSID", "value": "from-alias", "domain": DEFAULT_HOST, "path": "/"},
    ]
    forward = extract_cookies_from_storage(_storage(entries))
    reverse = extract_cookies_from_storage(_storage(list(reversed(entries))))

    assert {forward["OSID"], reverse["OSID"]} == {"from-legacy", "from-alias"}, (
        "same-tier duplicates must resolve by input order -- if this fails, the "
        "tier table changed and the docstrings describing it need updating too"
    )


def test_cross_tier_resolution_is_order_independent() -> None:
    """Negative control: distinct tiers resolve the same either way.

    Without this, an implementation that made *everything* order-dependent
    would satisfy the test above.
    """
    entries = [
        {"name": "OSID", "value": "from-wide", "domain": ".google.com", "path": "/"},
        {"name": "OSID", "value": "from-host", "domain": LEGACY_HOST, "path": "/"},
    ]
    forward = extract_cookies_from_storage(_storage(entries))
    reverse = extract_cookies_from_storage(_storage(list(reversed(entries))))

    assert forward["OSID"] == reverse["OSID"]


def test_flat_construction_degrades_to_broadcast() -> None:
    """A jar with no real domains cannot route -- and says so in the docstring.

    ``AuthTokens(cookies={flat})`` has no domains to preserve, so
    ``__post_init__`` widens every entry to ``.google.com`` and the "routed"
    header becomes the same broadcast for every host. Pinned because it is a
    silent degradation: ``cookie_header_for`` still returns a plausible header,
    just not a selective one. Callers get per-host selection only by building
    from ``storage_state``.
    """
    flat = AuthTokens(
        cookies={"SID": "v-sid", "OSID": "osid-only", "LSID": "v-lsid"},
        csrf_token="csrf",
        session_id="session",
    )
    app = _cookie_pairs(flat.cookie_header_for(f"https://{LEGACY_HOST}/x"))
    sign_in = _cookie_pairs(flat.cookie_header_for(f"https://{SIGN_IN_HOST}/x"))

    assert app == sign_in, "a domain-less jar broadcasts; it cannot select per host"
    assert ("LSID", "v-lsid") in app, "including cookies that should never reach the app host"


@pytest.mark.parametrize(
    ("bad_url", "match"),
    [("http://" + LEGACY_HOST + "/x", "https"), ("https:/typo", "host"), ("https://", "host")],
)
def test_malformed_urls_raise_rather_than_returning_empty(
    collided_auth: AuthTokens, bad_url: str, match: str
) -> None:
    """A URL that cannot match must raise, not return ``""``.

    ``""`` is a legitimate answer -- it means "no stored cookie is scoped to
    this host". So returning it for a *malformed* URL makes a typo
    indistinguishable from a correct negative, which is how a request ends up
    silently unauthenticated. ``https:/typo`` parses without error and matches
    nothing.
    """
    with pytest.raises(ValueError, match=match):
        collided_auth.cookie_header_for(bad_url)


def test_missing_jar_raises_rather_than_returning_empty() -> None:
    """No jar means the per-host question cannot be answered at all."""
    auth = AuthTokens(
        cookies={("SID", ".google.com", "/"): "v-sid"},
        csrf_token="csrf",
        session_id="session",
    )
    auth.cookie_jar = None
    with pytest.raises(ValueError, match="cookie jar"):
        auth.cookie_header_for(f"https://{LEGACY_HOST}/x")
