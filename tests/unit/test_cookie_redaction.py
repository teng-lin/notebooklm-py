"""Round-trip tests for the cassette cookie sanitizer.

The ``SENSITIVE_PATTERNS`` list in :mod:`tests/vcr_config.py` claims to redact
every Google session cookie before a cassette hits disk, but until this test
existed nothing actually proved that the regexes catch the names they enumerate.
This module feeds a Playwright-shaped ``storage_state`` JSON dict through
the ``scrub_string`` helper and asserts:

* every protected cookie name has its value scrubbed (no secret survives);
* scrubbing is idempotent (running twice yields the same output);
* unrelated cookie names (``BSID``, ``SAPISIDS``) are NOT scrubbed — i.e. the
  patterns are anchored tightly enough to avoid scrubbing legitimate fixture
  content;
* the ``__Secure-...`` family — including ``__Secure-1PSIDTS`` which the
  protected-name list does not enumerate explicitly — is caught by the generic
  ``__Secure-[^=]+`` (cookie-header form) / ``__Secure-[^"]+`` (JSON form)
  family patterns.

Regression context: until the corresponding sanitizer fix landed alongside this
test file, the existing patterns only matched the ``Cookie: SID=...; HSID=...``
header form and never fired on the JSON ``{"name": "SID", "value": "..."}``
storage_state form. A leaked ``storage_state.json`` therefore round-tripped
through the sanitizer untouched. The fix added two structural JSON patterns
(``name``-before-``value`` and the reversed defensive ordering) to
``SENSITIVE_PATTERNS`` in ``tests/vcr_config.py``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Add tests directory to path for vcr_config import (the tests package has no
# ``__init__.py`` so ``from tests.vcr_config import scrub_string`` would fail).
sys.path.insert(0, str(Path(__file__).parent.parent))

from vcr_config import scrub_string  # noqa: E402

# Protected cookie names that MUST be scrubbed in any cassette payload, in the
# specific JSON ``storage_state`` shape Playwright emits. The set matches the
# Tier-5 synthesis list (SID/SAPISID/HSID/SSID/APISID + the four ``__Secure-``
# 1P/3P SID and SIDCC variants).
PROTECTED: list[str] = [
    "SID",
    "SAPISID",
    "HSID",
    "SSID",
    "APISID",
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

    # Cross-check with the regex the Tier-5 plan called out: scan for any
    # surviving ``"value": "<original-secret>"`` pair.
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

    ``BSID`` and ``SAPISIDS`` are NOT in the Tier-5 synthesis protected list
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

    # Both names should still be present AND their values should be untouched.
    for name in benign:
        assert f'"name": "{name}"' in scrubbed
        # Value survives intact for these benign names.
        assert f'"name": "{name}", "value": "{SECRET}"' in scrubbed, (
            f"Cookie {name!r} value was unexpectedly scrubbed"
        )


def test_sidcc_remains_protected_in_storage_state() -> None:
    """Documents that ``SIDCC`` IS scrubbed via the JSON pattern.

    The Tier-5 plan notes ``SIDCC`` was not in the synthesis-listed protected
    set but is captured by the existing Cookie-header ``SIDCC=[^;]+`` regex.
    The structural JSON pattern added in this PR keeps ``SIDCC`` covered for
    consistency. This test pins that behavior so future edits to the pattern
    list don't silently flip it.
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
