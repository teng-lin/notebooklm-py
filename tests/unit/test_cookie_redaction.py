"""Round-trip tests for the cassette cookie sanitizer.

The ``SENSITIVE_PATTERNS`` list in :mod:`tests/vcr_config.py` claims to redact
every Google session cookie before a cassette hits disk, but until this test
existed nothing actually proved that the regexes catch the names they enumerate.
This module covers BOTH cookie shapes that show up in a VCR cassette payload:

1. **Playwright ``storage_state`` JSON shape**: ``{"cookies": [{"name": "SID",
   "value": "<secret>", "domain": ...}]}`` — what gets serialized when a
   storage_state dump leaks into a recorded response body.
2. **HTTP ``Cookie:`` header shape**: ``"SID=<secret>; SAPISID=<secret2>; ..."``
   — what the ``Cookie`` request header carries and what ``Set-Cookie`` response
   headers echo back.

For each shape the tests assert:

* every protected cookie name has its value scrubbed (no secret survives);
* scrubbing is idempotent (running twice yields the same output);
* unrelated cookie names (``BSID``, ``SAPISIDS``) are NOT scrubbed — i.e. the
  patterns are anchored tightly enough to avoid scrubbing legitimate fixture
  content;
* the ``__Secure-...`` family — including ``__Secure-1PSIDTS`` which the
  protected-name list does not enumerate explicitly — is caught by the generic
  ``__Secure-[^=]+`` (cookie-header form) / ``__Secure-[^"]+`` (JSON form)
  family patterns.

Regression context (two fixes landed alongside this test file):

* Pre-fix, the existing patterns only matched the ``Cookie: SID=...; HSID=...``
  header form and never fired on the JSON ``{"name": "SID", "value": "..."}``
  storage_state form. A leaked ``storage_state.json`` therefore round-tripped
  through the sanitizer untouched. The fix added two structural JSON patterns
  (``name``-before-``value`` and the reversed defensive ordering) to
  ``SENSITIVE_PATTERNS`` in ``tests/vcr_config.py``.
* Pre-fix, the header-form regexes were also unanchored, so a benign cookie
  named ``BSID`` was scrubbed as a side effect (its ``SID=...`` tail matched
  the protected ``SID=[^;]+`` regex). The fix prepends a negative lookbehind
  ``(?<![A-Za-z0-9_-])`` to each cookie-name regex so only cookie-name-boundary
  matches fire. ``SAPISID`` already escaped this because ``SAPISIDS`` doesn't
  contain a literal ``SAPISID=`` substring at the right boundary, but ``BSID``
  did, hence the explicit fix.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path

# Load ``tests/vcr_config.py`` via ``importlib`` rather than mutating
# ``sys.path``. The ``tests`` directory is not a package (no ``__init__.py``),
# so a plain ``from tests.vcr_config import scrub_string`` fails; a
# ``sys.path`` insertion would work but is module-load-time side-effectful and
# would silently shadow any future top-level module named ``vcr_config``.
# Loading by file path keeps the dependency localized to this test module.
_vcr_config_path = Path(__file__).resolve().parent.parent / "vcr_config.py"
_spec = importlib.util.spec_from_file_location("tests_vcr_config", _vcr_config_path)
assert _spec is not None and _spec.loader is not None, (
    f"Could not load tests/vcr_config.py from {_vcr_config_path}"
)
_vcr_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vcr_config)
scrub_string: Callable[[str], str] = _vcr_config.scrub_string

# Protected cookie names that MUST be scrubbed in any cassette payload, in the
# specific JSON ``storage_state`` shape Playwright emits. The set spans:
#
# * The core protected set (``SID``/``SAPISID``/``HSID``/``SSID``/``APISID``
#   + the four ``__Secure-`` 1P/3P SID variants) called out by the
#   cookie-redaction hardening.
# * ``OSID`` and ``NID`` — enumerated explicitly in the ``SENSITIVE_PATTERNS``
#   JSON regex, so exercising them in the round-trip closes a documentation
#   gap (the original protected list named neither for nor against them).
PROTECTED: list[str] = [
    "SID",
    "SAPISID",
    "HSID",
    "SSID",
    "APISID",
    "OSID",
    "NID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
]

# A sentinel secret string that would be catastrophic to leak: if any byte of it
# survives :func:`scrub_string`, the assertion below fails loudly. The value is
# distinctive enough that ``in`` substring matching is a reliable check.
SECRET = "this-is-a-real-secret-XYZ"


def _build_storage_state(names: list[str]) -> str:
    """Return a JSON ``storage_state`` dump containing one cookie per name."""
    storage_state = {
        "cookies": [
            {
                "name": name,
                "value": SECRET,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
            for name in names
        ],
        "origins": [],
    }
    return json.dumps(storage_state)


def test_storage_state_cookie_values_are_scrubbed() -> None:
    """No plaintext SID/APISID survives ``scrub_string`` on a storage_state dump.

    This is the load-bearing assertion of the PR: feed a Playwright-shaped
    JSON dump containing one cookie per protected name, sanitize it via the
    cassette pipeline's ``scrub_string`` helper, and confirm that the
    distinctive secret value appears zero times in the output. If any
    protected cookie's value survives, the secret string is recoverable
    from a committed cassette and this assertion fails.
    """
    dumped = _build_storage_state(PROTECTED)
    scrubbed = scrub_string(dumped)

    assert SECRET not in scrubbed, f"Plaintext secret survived scrubbing! Output:\n{scrubbed}"

    # Each protected cookie name itself MUST still be present — we redact the
    # value but keep the name so cassette diffs remain readable.
    for name in PROTECTED:
        assert f'"name": "{name}"' in scrubbed, (
            f"Cookie name {name!r} should be preserved in scrubbed output"
        )

    # Cross-check with the regex that the cookie-redaction hardening pinned:
    # scan for any surviving ``"value": "<original-secret>"`` pair.
    surviving = re.findall(r'"value":\s*"([^"]+)"', scrubbed)
    assert SECRET not in surviving, f"Found unredacted cookie values: {surviving!r}"


def test_scrub_string_is_idempotent_on_storage_state() -> None:
    """Running ``scrub_string`` twice yields a stable result.

    Idempotence matters because cassette recording can be triggered repeatedly
    during a single session (e.g. retries during E2E capture), and we never
    want a second pass to either un-redact or re-mangle a previously-redacted
    payload.
    """
    dumped = _build_storage_state(PROTECTED)
    once = scrub_string(dumped)
    twice = scrub_string(once)
    assert once == twice


def test_unrelated_cookie_names_are_not_scrubbed() -> None:
    """False-positive guard: cookies outside the protected set survive intact.

    ``BSID`` and ``SAPISIDS`` are NOT in the cookie-redaction protected list
    (and not in any existing ``SENSITIVE_PATTERNS`` regex, when those regexes
    are anchored to the JSON storage_state shape). Their values must round-trip
    unchanged so that a future contributor's legitimate fixture cookie isn't
    silently redacted.

    Note on the deliberate omission of ``SIDCC``: the existing patterns do list
    ``SIDCC`` as protected (in the Cookie-header form) and the JSON pattern
    added in this PR keeps it protected for consistency, so a separate test
    (:func:`test_sidcc_remains_protected_in_storage_state`) documents that
    behavior rather than treating ``SIDCC`` as a false-positive case.
    """
    benign = ["BSID", "SAPISIDS"]
    dumped = _build_storage_state(benign)
    scrubbed = scrub_string(dumped)

    # Parse the scrubbed payload back into a dict instead of substring matching
    # against the exact ``json.dumps`` separators. Substring matching would
    # silently pass if the serializer were ever switched to a pretty-printed
    # form (``json.dumps(..., indent=2)``); parsing checks the structural
    # invariant directly.
    parsed = json.loads(scrubbed)
    cookies_by_name = {c["name"]: c for c in parsed["cookies"]}
    for name in benign:
        assert name in cookies_by_name, f"Cookie name {name!r} disappeared after scrubbing"
        assert cookies_by_name[name]["value"] == SECRET, (
            f"Cookie {name!r} value was unexpectedly scrubbed: {cookies_by_name[name]['value']!r}"
        )


def test_sidcc_remains_protected_in_storage_state() -> None:
    """Documents that ``SIDCC`` IS scrubbed via the JSON pattern.

    ``SIDCC`` was not in the original core protected name list but is captured
    by the existing Cookie-header ``SIDCC=[^;]+`` regex. The structural JSON
    pattern added in this PR keeps ``SIDCC`` covered for consistency. This
    test pins that behavior so future edits to the pattern list don't
    silently flip it.
    """
    dumped = _build_storage_state(["SIDCC"])
    scrubbed = scrub_string(dumped)
    assert SECRET not in scrubbed, (
        "SIDCC value should be redacted by the JSON storage_state pattern"
    )


def test_secure_timestamp_cookie_is_scrubbed() -> None:
    """``__Secure-1PSIDTS`` is caught by the generic ``__Secure-[^"]+`` rule.

    The timestamp variant of the secure SID family is not enumerated by name
    in ``SENSITIVE_PATTERNS`` and there was previously no test verifying that
    the generic ``__Secure-...`` umbrella catches it. Lock that in here so a
    future refactor (e.g. tightening the umbrella to a literal name list)
    cannot silently re-expose it.
    """
    dumped = _build_storage_state(["__Secure-1PSIDTS"])
    scrubbed = scrub_string(dumped)

    assert SECRET not in scrubbed, f"__Secure-1PSIDTS value survived scrubbing:\n{scrubbed}"
    # Name itself is still readable (redact value, keep name).
    assert '"name": "__Secure-1PSIDTS"' in scrubbed


def test_host_umbrella_cookie_is_scrubbed_in_storage_state() -> None:
    """``__Host-*`` cookies are caught by the umbrella JSON pattern.

    Sister assertion to :func:`test_secure_timestamp_cookie_is_scrubbed`: the
    new JSON pattern includes ``__Host-[^"]+`` (mirroring the header-form
    ``__Host-[^=]+`` umbrella). Cookies in the ``__Host-`` family are scoped
    more strictly than ``__Secure-`` (no Domain attribute, must be served
    over HTTPS) but carry the same blast-radius if leaked, so the sanitizer
    treats them identically. Lock the umbrella in so a future tightening
    (e.g. switching to a literal name list) cannot silently re-expose them.
    """
    dumped = _build_storage_state(["__Host-GAPS"])
    scrubbed = scrub_string(dumped)

    assert SECRET not in scrubbed, f"__Host-GAPS value survived scrubbing:\n{scrubbed}"
    assert '"name": "__Host-GAPS"' in scrubbed


def test_storage_state_value_with_escaped_quote_does_not_leak_tail() -> None:
    """Adversarial cookie values containing escaped quotes are fully redacted.

    The naive value class ``[^"]*`` terminates at the first ``"`` in the input
    even when that quote is JSON-escaped (``\\"``), which would leave the
    portion of the value AFTER the escaped quote unredacted in the output.
    The PR fix uses the "string with escapes" idiom
    ``[^"\\\\]*(?:\\\\.[^"\\\\]*)*`` which consumes escape sequences correctly.

    This is a real (if narrow) attack surface: a service that ever returned a
    cookie value containing a quote, or a malicious fixture, would leak the
    tail of the value through the sanitizer. Pin the safe behavior so a
    future "simplification" of the regex cannot silently re-open it.
    """
    # The literal value embeds a quote character; ``json.dumps`` will escape it.
    raw_value = 'before"AFTER_LEAK_MARKER'
    payload = json.dumps(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": raw_value,
                    "domain": ".google.com",
                    "path": "/",
                }
            ],
            "origins": [],
        }
    )
    scrubbed = scrub_string(payload)

    # Neither the leading nor the trailing portion of the value should survive.
    assert "AFTER_LEAK_MARKER" not in scrubbed, (
        f"Tail-of-value leaked past escaped quote in:\n{scrubbed}"
    )
    assert "before" not in scrubbed, f"Head-of-value leaked before escaped quote in:\n{scrubbed}"
    # And the redaction marker should be present exactly once.
    assert scrubbed.count('"value": "SCRUBBED"') == 1


def test_storage_state_handles_value_before_name_ordering() -> None:
    """Defensive ordering: ``"value"`` appearing before ``"name"`` still scrubs.

    Playwright always emits ``name`` before ``value``, but hand-authored
    fixtures or a future Playwright version may reorder keys. The reversed
    pattern registered in ``SENSITIVE_PATTERNS`` should keep us safe either
    way.
    """
    # Construct JSON with the keys deliberately reversed.
    payload = (
        '{"cookies": [{"value": "' + SECRET + '", "name": "SID", '
        '"domain": ".google.com", "path": "/"}], "origins": []}'
    )
    scrubbed = scrub_string(payload)
    assert SECRET not in scrubbed, f"Value-before-name ordering leaked secret:\n{scrubbed}"


# =============================================================================
# HTTP ``Cookie:`` header shape — the second cookie payload form VCR records.
# =============================================================================
#
# VCR captures the request's ``Cookie`` header verbatim and recorded
# ``Set-Cookie`` response headers similarly. Both arrive at ``scrub_string`` as
# semicolon-delimited ``Name=Value`` pairs (no JSON wrapping). The patterns at
# the top of ``SENSITIVE_PATTERNS`` (``SID=[^;]+``, ``HSID=[^;]+``, ...) target
# exactly this shape — the tests below pin both the positive coverage (every
# protected SID is scrubbed) and the no-false-positive guarantee (``BSID``
# survives even though its name contains ``SID``).


def _build_cookie_header(names: list[str]) -> str:
    """Return an HTTP ``Cookie:``-style payload ``"<NAME>=<SECRET>; ..."``."""
    return "; ".join(f"{name}={SECRET}" for name in names)


def test_cookie_header_values_are_scrubbed() -> None:
    """No plaintext SID/APISID survives ``scrub_string`` on a Cookie header.

    Sister assertion to :func:`test_storage_state_cookie_values_are_scrubbed`,
    but for the semicolon-delimited header shape rather than JSON.
    """
    header = _build_cookie_header(PROTECTED)
    scrubbed = scrub_string(header)

    assert SECRET not in scrubbed, (
        f"Plaintext secret survived Cookie-header scrubbing! Output:\n{scrubbed}"
    )
    # Every protected name should appear with ``=SCRUBBED`` after it.
    for name in PROTECTED:
        assert f"{name}=SCRUBBED" in scrubbed, f"Cookie {name!r} was not scrubbed in header form"


def test_cookie_header_scrub_is_idempotent() -> None:
    """Header-form ``scrub_string`` is idempotent (no oscillation between passes)."""
    header = _build_cookie_header(PROTECTED)
    once = scrub_string(header)
    twice = scrub_string(once)
    assert once == twice


def test_cookie_header_does_not_scrub_unrelated_names() -> None:
    """False-positive guard for the header form.

    ``BSID`` and ``SAPISIDS`` must survive untouched. ``BSID`` is the
    historically buggy case: before the negative-lookbehind anchor landed in
    ``SENSITIVE_PATTERNS``, the unanchored ``SID=[^;]+`` regex matched the
    ``SID=...`` tail of ``BSID=...`` and corrupted the value. ``SAPISIDS``
    already worked correctly (the literal ``SAPISID=`` substring doesn't
    appear in ``SAPISIDS=value``) but is included as a defensive companion.
    """
    benign = ["BSID", "SAPISIDS"]
    header = _build_cookie_header(benign)
    scrubbed = scrub_string(header)

    for name in benign:
        assert f"{name}={SECRET}" in scrubbed, (
            f"Cookie {name!r} value was unexpectedly scrubbed (header form): {scrubbed}"
        )


def test_cookie_header_sidcc_remains_protected() -> None:
    """Documents that ``SIDCC`` IS scrubbed by the existing header pattern.

    Symmetric to :func:`test_sidcc_remains_protected_in_storage_state` for
    the header form.
    """
    header = _build_cookie_header(["SIDCC"])
    scrubbed = scrub_string(header)
    assert SECRET not in scrubbed, "SIDCC value should be redacted by the Cookie-header pattern"


def test_cookie_header_secure_timestamp_cookie_is_scrubbed() -> None:
    """``__Secure-1PSIDTS`` is caught by the generic ``__Secure-[^=]+`` rule.

    Sister assertion to :func:`test_secure_timestamp_cookie_is_scrubbed` for
    the header form. The umbrella ``__Secure-[^=]+`` pattern already covers
    every ``__Secure-...`` variant Google ships; this test pins that so a
    future tightening (e.g. switching to a literal name list) cannot silently
    re-expose the timestamp cookie.
    """
    header = _build_cookie_header(["__Secure-1PSIDTS"])
    scrubbed = scrub_string(header)
    assert SECRET not in scrubbed
    assert "__Secure-1PSIDTS=SCRUBBED" in scrubbed


def test_cookie_header_host_umbrella_is_scrubbed() -> None:
    """``__Host-*`` cookies are caught by the header-form umbrella.

    Sister assertion to :func:`test_host_umbrella_cookie_is_scrubbed_in_storage_state`
    for the header form.
    """
    header = _build_cookie_header(["__Host-GAPS"])
    scrubbed = scrub_string(header)
    assert SECRET not in scrubbed
    assert "__Host-GAPS=SCRUBBED" in scrubbed


def test_cookie_header_realistic_set_cookie_payload() -> None:
    """End-to-end shape: a realistic ``Set-Cookie``-style payload is sanitized.

    VCR records ``Set-Cookie`` headers as full cookie attribute strings, e.g.
    ``"SID=<secret>; Path=/; Domain=.google.com; Secure; HttpOnly"``. The
    regex must scrub the value without disturbing the trailing attributes —
    the ``[^;]+`` value class is what guarantees this, so we exercise it
    explicitly here.
    """
    payload = (
        f"SID={SECRET}; Path=/; Domain=.google.com; Secure; HttpOnly, "
        f"__Secure-1PSID={SECRET}; Path=/; Domain=.google.com; Secure; HttpOnly"
    )
    scrubbed = scrub_string(payload)
    assert SECRET not in scrubbed
    # Cookie attributes after the value must survive untouched.
    assert "Path=/" in scrubbed
    assert "Domain=.google.com" in scrubbed
    assert "Secure" in scrubbed
    assert "HttpOnly" in scrubbed
