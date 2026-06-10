"""Parity guard: runtime redaction registry stays in lockstep with the cassette
sanitizer on EVERY must-scrub axis (cookie names, secure/host umbrellas, and
credential shapes).

The runtime secret registry (``src/notebooklm/_secrets.py``) is the single
source of truth that the logging redaction patterns and exception scrubbing
derive from. The cassette sanitizer (``tests/cassette_patterns.py``) is the
separate, test-only source of truth for what counts as a must-scrub credential
in a recorded HTTP cassette.

Runtime code cannot import from ``tests/`` (the test tree is not shipped), so
the two registries are necessarily re-stated. This guard closes the drift that
motivated issue #1517 and the three findings from its codex security review:

1. **Bare session-cookie names.** The runtime set must be a SUPERSET of the
   cassette registry's bare ``SESSION_COOKIES``. ``NID`` / ``LSOLH`` were on the
   cassette must-scrub list but ABSENT from the runtime alternation, so a bare
   ``NID=g.a000-...`` token logged at DEBUG round-tripped verbatim through
   ``scrub_secrets``.

2. **``__Secure-*`` / ``__Host-*`` umbrellas.** Rather than enumerate every
   secure/host name, the runtime registry redacts them by prefix umbrella so a
   FUTURE name (``SECURE_COOKIES.append("__Secure-NEWSESSION")``) fails closed
   by construction. This test proves every cassette ``SECURE_COOKIES`` /
   ``HOST_COOKIES`` name is actually matched by the runtime umbrella regex —
   so the umbrella can never silently stop covering one.

3. **Credential shapes.** The runtime ``AUTH_TOKEN_SHAPE_PATTERNS`` must be
   regex-string-EQUAL to the cassette registry's token-shape patterns plus its
   Google API-key shape. If the cassette registry gains a fourth shape (e.g.
   an ``oauth2rt_…`` refresh-token pattern), this test fails until the runtime
   registry lists it too — closing the gap where runtime would silently miss a
   newly-recognized credential family in the refresh-DEBUG / ``data_at_failure``
   sinks.
"""

from __future__ import annotations

import re

from notebooklm._secrets import (
    AUTH_TOKEN_SHAPE_PATTERNS,
    RUNTIME_SESSION_COOKIES,
    SECURE_HOST_UMBRELLA_PATTERNS,
)
from tests.cassette_patterns import (
    _AUTH_TOKEN_PATTERNS,
    _GOOGLE_API_KEY_PATTERN,
    HOST_COOKIES,
    SECURE_COOKIES,
    SESSION_COOKIES,
)


def test_runtime_redaction_superset_of_cassette_bare_session_cookies() -> None:
    """Every cassette bare ``SESSION_COOKIES`` name is in the runtime set.

    ``SESSION_COOKIES`` is the cassette registry's list of bare (non-prefixed)
    cookie names whose VALUES must never survive into a committed cassette; the
    same names must be redacted at RUNTIME (log lines, exception surfaces) or a
    token carried by one of them leaks through ``scrub_secrets``. ``__Secure-*``
    / ``__Host-*`` names are covered by the umbrella test below instead.
    """
    runtime = set(RUNTIME_SESSION_COOKIES)
    missing = set(SESSION_COOKIES) - runtime
    assert missing == set(), (
        "Runtime redaction registry (notebooklm._secrets.RUNTIME_SESSION_COOKIES) "
        "is missing bare cookie name(s) the cassette sanitizer "
        "(tests/cassette_patterns.py SESSION_COOKIES) classifies as must-scrub. A "
        "token carried by these would leak through scrub_secrets (issue #1517). "
        f"Add them to RUNTIME_SESSION_COOKIES: {sorted(missing)}"
    )


def test_runtime_umbrella_covers_every_cassette_secure_and_host_cookie() -> None:
    """The ``__Secure-*`` / ``__Host-*`` umbrellas match every cassette name.

    Proves the prefix-umbrella approach (codex review finding 3) actually covers
    each enumerated ``SECURE_COOKIES`` / ``HOST_COOKIES`` name — and, because it
    matches by prefix, any FUTURE name with the same prefix too. A regression
    (e.g. tightening the umbrella so it stops matching a real name) fails here.
    """
    umbrellas = [re.compile(p) for p in SECURE_HOST_UMBRELLA_PATTERNS]
    for name in (*SECURE_COOKIES, *HOST_COOKIES):
        # Both the bare and the RFC 6265 double-quoted value forms must match —
        # a quoted value (``__Secure-X="opaque"``) leaked before the umbrella
        # gained the optional surrounding quotes (gemini review of #1530).
        for sample in (
            f"{name}=opaqueOAuthOpaqueValue1234567890",
            f'{name}="opaqueOAuthOpaqueValue1234567890"',
        ):
            assert any(u.search(sample) for u in umbrellas), (
                f"Cassette secure/host cookie {name!r} is NOT matched by any "
                "runtime umbrella in "
                "notebooklm._secrets.SECURE_HOST_UMBRELLA_PATTERNS for sample "
                f"{sample!r}. A value carried by it would leak through "
                "scrub_secrets (codex review of #1517 / gemini review of #1530)."
            )


def test_runtime_credential_shapes_equal_cassette_shapes() -> None:
    """Runtime shape patterns are regex-string-equal to the cassette set.

    The cassette registry's ``_AUTH_TOKEN_PATTERNS`` + ``_GOOGLE_API_KEY_PATTERN``
    are the canonical Google credential shapes (``g.a000-`` / ``sidts-`` /
    ``ya29.`` tokens + the ``AIza…`` API key). ``AUTH_TOKEN_SHAPE_PATTERNS`` must
    list the SAME regex strings so the runtime redaction (refresh-cmd DEBUG sink,
    ``data_at_failure`` / ``payload_preview`` exception surfaces) cannot drift
    from the cassette scrubber. Set equality (not order) is asserted; if the
    cassette registry gains a new shape, add the identical regex here.
    """
    cassette_shapes = {*_AUTH_TOKEN_PATTERNS, _GOOGLE_API_KEY_PATTERN}
    runtime_shapes = set(AUTH_TOKEN_SHAPE_PATTERNS)
    missing = cassette_shapes - runtime_shapes
    extra = runtime_shapes - cassette_shapes
    assert missing == set() and extra == set(), (
        "notebooklm._secrets.AUTH_TOKEN_SHAPE_PATTERNS has drifted from the "
        "cassette registry's credential-shape set (tests/cassette_patterns.py "
        "_AUTH_TOKEN_PATTERNS + _GOOGLE_API_KEY_PATTERN). If the cassette registry "
        "gained a shape, add the IDENTICAL regex string to the runtime registry. "
        f"missing-from-runtime={sorted(missing)} extra-in-runtime={sorted(extra)}"
    )


def test_runtime_registry_regression_anchors_present() -> None:
    """Pin the #1517 leak carriers so a future trim can't silently drop them.

    The axis tests above would also catch a removal, but this explicit anchor
    documents WHY each is load-bearing: every name/shape below was a confirmed
    (or reviewer-flagged) runtime leak carrier.
    """
    runtime_cookies = set(RUNTIME_SESSION_COOKIES)
    for name in ("NID", "LSOLH", "OSID"):
        assert name in runtime_cookies, (
            f"{name!r} dropped from RUNTIME_SESSION_COOKIES (issue #1517)"
        )
    runtime_shapes = set(AUTH_TOKEN_SHAPE_PATTERNS)
    # The Google API-key shape is the codex-review finding-1 leak carrier; pin it.
    assert r"AIza[0-9A-Za-z_\-]{35,}" in runtime_shapes, (
        "Google API-key shape dropped from AUTH_TOKEN_SHAPE_PATTERNS (codex #1517 finding)"
    )
