"""Regression tests for the catch-all Google auth-token scrubbing.

Context — the ``LSID`` / ``g.a000-...`` leak
---------------------------------------------
The VCR cassette scrubber (:mod:`tests.cassette_patterns`) historically relied
on a per-cookie-name allowlist (:data:`SESSION_COOKIES` + the ``__Secure-*`` /
``__Host-*`` umbrellas). The Google **login** cookie ``LSID`` was missing from
that allowlist, so its value — which embeds a raw ``g.a000-...`` SID token, the
same credential family as ``SID`` — round-tripped into a committed cassette
(``tests/cassettes/notebooks_share.yaml``) unscrubbed. That is a live-session
credential leak in a public repo.

Two complementary hardenings closed the gap and are pinned here:

1. **Allowlist completion.** ``LSID`` (and its sibling ``LSOLH``) are now in
   :data:`SESSION_COOKIES`, so the cookie-header / storage_state scrubbers
   collapse their values to ``SCRUBBED`` like every other session cookie.
2. **Catch-all token regexes (defense in depth).** ``scrub_string`` now scrubs
   the raw Google credential shapes ``g.a000-...`` / ``sidts-...`` / ``ya29....``
   wherever they appear — request/response BODIES, HEADERS, and cookie values
   carried by a name that is NOT on the allowlist. This backstop never depends
   on a cookie name being enumerated, so a future unknown login cookie cannot
   re-open the same hole.

The companion validator ``is_clean`` carries a matching detector so the CI
cassette guard (``tests/scripts/check_cassettes_clean.py``) flags any of these
token shapes that ever survive into a committed cassette.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Load ``tests/cassette_patterns.py`` by file path to keep the dependency
# localized to this test module.
_patterns_path = Path(__file__).resolve().parent.parent / "cassette_patterns.py"
_spec = importlib.util.spec_from_file_location("tests_cassette_patterns_scrub", _patterns_path)
assert _spec is not None and _spec.loader is not None, (
    f"Could not load tests/cassette_patterns.py from {_patterns_path}"
)
_patterns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_patterns)
scrub_string: Callable[[str], str] = _patterns.scrub_string
is_clean: Callable[[str], tuple[bool, list[str]]] = _patterns.is_clean
SESSION_COOKIES: list[str] = _patterns.SESSION_COOKIES


# A distinctive raw SID token tail. If any byte of it survives ``scrub_string``,
# the assertion fails loudly — it is the credential we must never leak.
_G_A000 = "g.a000-AhleFsSECRET_TOKEN_TAIL_abc123_DEF456"
_G_A000_2 = "g.a000-SECOND_SECRET_TOKEN_xyz789"


def test_cookie_header_with_lsid_and_secure_psid_fully_scrubbed() -> None:
    """The exact leak shape — ``LSID=...g.a000-...; __Secure-1PSID=g.a000-...``.

    Reproduces the ``notebooks_share.yaml`` cookie header and asserts that:

    * no ``g.a000`` fragment survives anywhere in the output;
    * the ``LSID``-value secret is gone (the allowlist now covers ``LSID``);
    * the ``SCRUBBED`` placeholder IS present (the value was redacted, not
      merely dropped);
    * ``is_clean`` flags the RAW header as a leak and accepts the scrubbed form.
    """
    header = (
        f"__Host-GAPS=somegapsvalue; LSID=o.myaccount.google.com|o.x:{_G_A000}; "
        f"__Secure-1PSID={_G_A000_2}; NID=benign"
    )

    scrubbed = scrub_string(header)

    assert "g.a000" not in scrubbed, f"g.a000 token leaked through scrubbing:\n{scrubbed}"
    assert "SECRET" not in scrubbed, f"secret fragment survived:\n{scrubbed}"
    assert "SCRUBBED" in scrubbed, "expected the SCRUBBED placeholder to be present"
    assert "LSID=SCRUBBED" in scrubbed, f"LSID value was not scrubbed:\n{scrubbed}"

    ok_raw, leaks_raw = is_clean(header)
    assert not ok_raw, "is_clean must flag the raw leaked cookie header"
    assert leaks_raw, "is_clean must report at least one leak for the raw header"

    ok_scrubbed, leaks_scrubbed = is_clean(scrubbed)
    assert ok_scrubbed, f"scrubbed header still flagged as leaking: {leaks_scrubbed}"


def test_lsid_is_on_the_session_cookie_allowlist() -> None:
    """Pin ``LSID`` (and ``LSOLH``) onto the allowlist — the root-cause fix.

    A future refactor that drops ``LSID`` from :data:`SESSION_COOKIES` would
    silently re-open the leak for cookie values that don't happen to carry one
    of the catch-all token shapes. Lock it in.
    """
    assert "LSID" in SESSION_COOKIES
    assert "LSOLH" in SESSION_COOKIES


def test_lsid_storage_state_json_value_is_fully_scrubbed() -> None:
    """``LSID`` in the Playwright ``storage_state`` JSON shape is fully scrubbed.

    Regression for the JSON-scrubber drift: the storage_state / JSON-key cookie
    scrubbers used to hard-code the old cookie-name set, so a
    ``{"name":"LSID","value":"<non-token-secret>"}`` object would only be
    partially scrubbed and ``is_clean`` (whose detector IS registry-derived)
    would then flag the residual value as a leak. Both the scrubber and the
    detector are now derived from ``SESSION_COOKIES`` so the two stay in sync.

    Uses a NON-``g.a000`` secret value on purpose so the catch-all token regex
    can't mask a gap in the JSON-shape cookie scrubber.
    """
    secret = "this-storage-state-secret-must-not-survive"
    # name-before-value (what Playwright emits) and the bare-JSON-key shape.
    name_first = f'{{"name": "LSID", "value": "{secret}", "domain": ".google.com"}}'
    json_key = f'{{"LSID": "{secret}", "LSOLH": "{secret}"}}'

    for payload in (name_first, json_key):
        scrubbed = scrub_string(payload)
        assert secret not in scrubbed, f"LSID JSON value leaked:\n{scrubbed}"
        ok, leaks = is_clean(scrubbed)
        assert ok, f"scrubbed JSON cookie shape still flagged: {leaks}"


def test_g_a000_token_in_body_is_scrubbed() -> None:
    """A ``g.a000-...`` token embedded in a response BODY is scrubbed.

    The cookie-name-anchored patterns never fire on a JSON body field, so this
    exercises the catch-all token regex directly. The token must not survive
    and the scrubbed body must pass ``is_clean``.
    """
    body = (
        '{"refresh":"' + _G_A000 + '","nested":{"sid":"' + _G_A000_2 + '"},"keep":"ordinary-value"}'
    )

    scrubbed = scrub_string(body)

    assert "g.a000" not in scrubbed, f"g.a000 token leaked from body:\n{scrubbed}"
    assert "SECRET" not in scrubbed
    assert "ordinary-value" in scrubbed, "non-secret body content must be preserved"

    ok, leaks = is_clean(scrubbed)
    assert ok, f"scrubbed body still flagged: {leaks}"

    ok_raw, _ = is_clean(body)
    assert not ok_raw, "is_clean must flag the raw token-carrying body"


def test_sidts_and_ya29_token_shapes_are_scrubbed() -> None:
    """The ``sidts-...`` rotation token and ``ya29....`` OAuth token are scrubbed."""
    sidts = "sidts-CjEBSECRET1234567890abcdefghij"
    ya29 = "ya29.A0AfH6SMBverylongaccesstoken_1234567890abcdef"
    text = f'{{"ts":"{sidts}","access_token":"{ya29}"}}'

    scrubbed = scrub_string(text)

    assert "sidts-" not in scrubbed, f"sidts token leaked:\n{scrubbed}"
    assert "ya29." not in scrubbed, f"ya29 token leaked:\n{scrubbed}"
    assert "SECRET" not in scrubbed

    ok, leaks = is_clean(scrubbed)
    assert ok, f"scrubbed output still flagged: {leaks}"


def test_token_scrub_is_idempotent() -> None:
    """Running the scrubber twice over token-bearing content is stable."""
    header = f"LSID=o.x:{_G_A000}; __Secure-1PSID={_G_A000_2}"
    once = scrub_string(header)
    twice = scrub_string(once)
    assert once == twice


def test_token_scrub_does_not_clobber_benign_lookalikes() -> None:
    """False-positive guard for the catch-all token regexes.

    Short incidental literals (``ya29`` with no token tail, a bare ``g.a000``
    account prefix with no hyphen tail, ``sidts`` with too short a tail) must
    NOT be scrubbed — only the high-entropy token forms collapse. Pinning this
    keeps the catch-all from corrupting legitimate fixture content.
    """
    benign = "label=ya29 prefix=g.a000 marker=sidts plain=hello-world"
    scrubbed = scrub_string(benign)
    assert scrubbed == benign, f"benign lookalikes were clobbered:\n{scrubbed}"


def test_committed_share_cassette_is_clean() -> None:
    """End-to-end: the previously-leaked cassette now passes ``is_clean``.

    Guards against a regression where the cassette is re-recorded (or
    hand-edited) and the ``g.a000`` token sneaks back in. Reads the on-disk
    file the leak originally lived in.
    """
    cassette = Path(__file__).resolve().parent.parent / "cassettes" / "notebooks_share.yaml"
    if not cassette.exists():  # pragma: no cover - cassette is committed
        return
    text = cassette.read_text(encoding="utf-8")
    assert "g.a000" not in text, "g.a000 token is present in the committed share cassette"
    ok, leaks = is_clean(text)
    assert ok, f"notebooks_share.yaml still contains leaks: {leaks[:5]}"


def test_scrub_response_helper_scrubs_set_cookie_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``scrub_response`` (the VCR hook) scrubs a token in a Set-Cookie header.

    Loads the VCR config module and drives its response-scrub hook with a
    Set-Cookie header carrying a raw ``g.a000`` token, confirming the wiring
    end-to-end (not just the underlying ``scrub_string``).

    ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is cleared so an env var leaking from the
    test runner cannot make ``scrub_response`` substitute a synthetic-error
    body and silently change the shape this test asserts on.
    """
    monkeypatch.delenv("NOTEBOOKLM_VCR_RECORD_ERRORS", raising=False)
    vcr_config_path = Path(__file__).resolve().parent.parent / "vcr_config.py"
    spec = importlib.util.spec_from_file_location("tests_vcr_config_scrub", vcr_config_path)
    assert spec is not None and spec.loader is not None
    vcr_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vcr_config)

    response: dict[str, Any] = {
        "status": {"code": 200, "message": "OK"},
        "headers": {"Set-Cookie": [f"LSID=o.x:{_G_A000}; Path=/; Secure; HttpOnly"]},
        "body": {"string": b"[]"},
    }
    out = vcr_config.scrub_response(response)
    set_cookie = out["headers"]["Set-Cookie"]
    joined = "".join(set_cookie) if isinstance(set_cookie, list) else set_cookie
    assert "g.a000" not in joined, f"Set-Cookie token leaked:\n{joined}"
    assert "LSID=SCRUBBED" in joined


def test_scrub_response_scrubs_lowercase_set_cookie_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``scrub_response`` scrubs a lowercase ``set-cookie`` header (HTTP/2).

    Google serves HTTP/2, whose header names are lowercase, so a fresh recording
    carries ``set-cookie`` (not the title-case ``Set-Cookie`` older cassettes
    have). A case-sensitive lookup would leave the live session token unscrubbed
    — this pins the case-insensitive match.
    """
    monkeypatch.delenv("NOTEBOOKLM_VCR_RECORD_ERRORS", raising=False)
    vcr_config_path = Path(__file__).resolve().parent.parent / "vcr_config.py"
    spec = importlib.util.spec_from_file_location("tests_vcr_config_scrub_ci", vcr_config_path)
    assert spec is not None and spec.loader is not None
    vcr_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vcr_config)

    response: dict[str, Any] = {
        "status": {"code": 200, "message": "OK"},
        "headers": {"set-cookie": [f"SIDCC={_G_A000}; expires=Sun, 27-Jun-2027; path=/; Secure"]},
        "body": {"string": b"[]"},
    }
    out = vcr_config.scrub_response(response)
    set_cookie = out["headers"]["set-cookie"]
    joined = "".join(set_cookie) if isinstance(set_cookie, list) else set_cookie
    assert "g.a000" not in joined, f"lowercase set-cookie token leaked:\n{joined}"
    assert "SIDCC=SCRUBBED" in joined
