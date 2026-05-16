"""Tests for the canonical cassette sanitization registry.

The registry lives in :mod:`tests.cassette_patterns` and exports a single
:func:`scrub_string` sanitizer plus an :func:`is_clean` validator. These
tests assert:

- Every cookie shape we know about scrubs cleanly (positive)
- Scrubbing is idempotent on already-scrubbed input (no double-scrub)
- Every placeholder in ``SCRUB_PLACEHOLDERS`` is recognised as clean
- A real cookie value starting with ``S`` IS still flagged as a leak —
  the legacy bash guard used a ``[^S"]`` character class that exempted
  any real secret whose first character was ``S``; the consolidated
  registry uses exact-match placeholder membership to close that hole.
- Registry stays in sync with :mod:`tests.vcr_config`
- Bad-cassette regressions (the shape-lint inputs) are caught by
  :func:`is_clean`. Inline payloads are used so this test does not depend
  on filesystem fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ``tests/cassette_patterns.py`` lives directly under ``tests/`` (not in a
# package). Other test modules add it to ``sys.path``; we follow the same
# convention so the validator is importable in either layout.
REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
sys.path.insert(0, str(TESTS_DIR))

import vcr_config  # noqa: E402
from cassette_patterns import (  # noqa: E402
    DISPLAY_NAME_FALSE_POSITIVES,
    EMAIL_PROVIDERS,
    HOST_COOKIES,
    OPTIONAL_COOKIES,
    SCRUB_PLACEHOLDERS,
    SECURE_COOKIES,
    SESSION_COOKIES,
    is_clean,
    scrub_string,
)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def test_registry_exports_required_constants() -> None:
    """Every constant called out in the sanitization-registry spec is exported."""
    assert isinstance(SESSION_COOKIES, list) and SESSION_COOKIES
    assert isinstance(SECURE_COOKIES, list) and SECURE_COOKIES
    assert isinstance(HOST_COOKIES, list) and HOST_COOKIES
    assert isinstance(OPTIONAL_COOKIES, list)  # allowed empty in theory
    assert isinstance(EMAIL_PROVIDERS, list) and EMAIL_PROVIDERS
    assert isinstance(SCRUB_PLACEHOLDERS, frozenset) and SCRUB_PLACEHOLDERS


def test_session_cookies_contains_expected_names() -> None:
    """Lock the canonical SID-family cookie names."""
    for name in ("SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC", "OSID", "NID"):
        assert name in SESSION_COOKIES


# ---------------------------------------------------------------------------
# scrub_string — positive: every cookie shape we redact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC", "OSID", "NID"]
)
def test_session_cookie_header_form_is_scrubbed(name: str) -> None:
    """``Cookie: NAME=secret; ...`` → ``Cookie: NAME=SCRUBBED; ...``"""
    header = f"Cookie: foo=bar; {name}=ABCDEF1234567890; baz=qux"
    scrubbed = scrub_string(header)
    assert "ABCDEF1234567890" not in scrubbed
    assert f"{name}=SCRUBBED" in scrubbed


@pytest.mark.parametrize(
    "name",
    [
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDTS",
        "__Host-GAPS",
    ],
)
def test_secure_and_host_cookies_are_scrubbed(name: str) -> None:
    """``__Secure-*`` / ``__Host-*`` umbrella scrubs the value, keeps the name."""
    header = f"Cookie: {name}=REAL_SECRET_HERE; other=keep"
    scrubbed = scrub_string(header)
    assert "REAL_SECRET_HERE" not in scrubbed
    assert f"{name}=SCRUBBED" in scrubbed


@pytest.mark.parametrize("name", ["SID", "SAPISID", "__Secure-1PSID"])
def test_storage_state_name_first_is_scrubbed(name: str) -> None:
    """Playwright storage_state ``{"name":..., "value":...}`` shape is scrubbed."""
    text = f'{{"name":"{name}","value":"REAL_VALUE_HERE","domain":".google.com"}}'
    scrubbed = scrub_string(text)
    assert "REAL_VALUE_HERE" not in scrubbed
    assert '"value":"SCRUBBED"' in scrubbed


@pytest.mark.parametrize("name", ["SID", "SAPISID", "__Secure-1PSID"])
def test_storage_state_value_first_is_scrubbed(name: str) -> None:
    """Defensive ordering: ``"value":..., "name":...`` is also scrubbed."""
    text = f'{{"value":"REAL_VALUE_HERE","name":"{name}","domain":".google.com"}}'
    scrubbed = scrub_string(text)
    assert "REAL_VALUE_HERE" not in scrubbed
    assert '"value":"SCRUBBED"' in scrubbed


def test_url_session_id_is_scrubbed() -> None:
    """``f.sid=...`` in URL query params is scrubbed."""
    url = "https://notebooklm.google.com/_/?f.sid=ABCDE12345&f.cv=1"
    assert "ABCDE12345" not in scrub_string(url)
    assert "f.sid=SCRUBBED" in scrub_string(url)


def test_at_csrf_token_is_scrubbed_in_body() -> None:
    """``at=...`` form parameter is scrubbed to ``at=SCRUBBED_CSRF``."""
    body = "f.req=..&at=AABBCCDD-EEFF-GGHH&other=keep"
    scrubbed = scrub_string(body)
    assert "AABBCCDD-EEFF-GGHH" not in scrubbed
    assert "at=SCRUBBED_CSRF" in scrubbed


@pytest.mark.parametrize("provider", EMAIL_PROVIDERS)
def test_email_is_scrubbed_quoted(provider: str) -> None:
    """JSON-quoted emails at any supported provider are scrubbed."""
    text = f'{{"email":"alice.example+tag@{provider}.com"}}'
    scrubbed = scrub_string(text)
    assert provider not in scrubbed
    assert '"SCRUBBED_EMAIL@example.com"' in scrubbed


@pytest.mark.parametrize("provider", EMAIL_PROVIDERS)
def test_email_is_scrubbed_unquoted(provider: str) -> None:
    """Bare ``user@provider.com`` in HTML / JS contexts is scrubbed."""
    text = f'<a href="mailto:alice.example+tag@{provider}.com">mail me</a>'
    scrubbed = scrub_string(text)
    assert provider not in scrubbed
    assert "SCRUBBED_EMAIL@example.com" in scrubbed


# ---------------------------------------------------------------------------
# scrub_string — negative: legitimate content survives unchanged
# ---------------------------------------------------------------------------


def test_bsid_cookie_substring_is_not_scrubbed() -> None:
    """A benign cookie named ``BSID`` containing the ``SID`` suffix survives.

    The negative lookbehind on each cookie-header pattern anchors at a
    cookie-name boundary; without it the regex would eat the ``SID=...`` tail
    of ``BSID=...``.
    """
    header = "Cookie: BSID=PUBLIC_VALUE_HERE; other=keep"
    assert scrub_string(header) == header


def test_legitimate_two_word_source_title_not_scrubbed() -> None:
    """A non-displayName JSON key with a two-Capitalized-word value survives."""
    text = '{"title": "Source Title"}'
    assert scrub_string(text) == text


def test_unknown_email_provider_not_scrubbed() -> None:
    """An email at a provider we do NOT cover (``@corp.internal``) is preserved."""
    text = '{"contact":"bob@corp.internal"}'
    assert scrub_string(text) == text


@pytest.mark.parametrize("param", ["flat", "rate", "format", "stat"])
def test_at_lookbehind_protects_param_names_ending_in_at(param: str) -> None:
    """Params whose names *end* in ``at`` are not eaten by the ``at=`` scrubber.

    Without the negative-lookbehind anchor, ``at=[A-Za-z0-9_-]+`` would match
    the substring ``at=VALUE`` inside ``flat=VALUE`` / ``rate=VALUE`` and
    corrupt the URL or form body.
    """
    body = f"foo=1&{param}=PUBLIC_VALUE&bar=2"
    assert scrub_string(body) == body


# ---------------------------------------------------------------------------
# scrub_string — idempotence
# ---------------------------------------------------------------------------


def test_scrub_is_idempotent_on_already_scrubbed_cookie_header() -> None:
    text = "Cookie: SID=SCRUBBED; __Secure-1PSID=SCRUBBED"
    once = scrub_string(text)
    twice = scrub_string(once)
    assert once == twice
    assert once == text  # nothing changed on the first pass either


def test_scrub_is_idempotent_on_already_scrubbed_email() -> None:
    """``SCRUBBED_EMAIL@example.com`` survives a second scrub pass unchanged."""
    once = scrub_string('{"email":"alice@gmail.com"}')
    twice = scrub_string(once)
    assert once == twice
    assert '"SCRUBBED_EMAIL@example.com"' in twice


def test_scrub_is_idempotent_on_already_scrubbed_storage_state() -> None:
    text = '{"name":"SID","value":"SCRUBBED","domain":".google.com"}'
    once = scrub_string(text)
    twice = scrub_string(once)
    assert once == twice == text


@pytest.mark.parametrize(
    "field,placeholder",
    [
        ("SNlM0e", "SCRUBBED_CSRF"),
        ("FdrFJe", "SCRUBBED_SESSION"),
        ("oPEP7c", "SCRUBBED_EMAIL"),
        ("S06Grb", "SCRUBBED_USER_ID"),
        ("B8SWKb", "SCRUBBED_API_KEY"),
        ("at", "SCRUBBED_CSRF"),
    ],
)
def test_token_field_scrubs_value_with_escaped_quote(field: str, placeholder: str) -> None:
    """JSON token values containing ``\\"`` are scrubbed in full, not truncated.

    Regression test for the naive ``[^"]+`` value match: without the escape-
    aware idiom, the scrub stops at the first ``\\"`` and leaves the suffix of
    the secret in the cassette while ``is_clean`` is fooled by the leading
    placeholder. The new ``(?:[^"\\\\]|\\\\.)*`` idiom matches across escape
    sequences so the entire JSON string value is replaced.
    """
    text = f'{{"{field}":"REAL_PREFIX\\"REAL_SUFFIX"}}'
    scrubbed = scrub_string(text)
    assert "REAL_PREFIX" not in scrubbed, scrubbed
    assert "REAL_SUFFIX" not in scrubbed, scrubbed
    assert f'"{field}":"{placeholder}"' in scrubbed


# ---------------------------------------------------------------------------
# is_clean — positive: every known placeholder is accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", sorted(SCRUB_PLACEHOLDERS))
def test_is_clean_accepts_known_placeholder_in_cookie_header(placeholder: str) -> None:
    """Every member of ``SCRUB_PLACEHOLDERS`` is recognised as a clean value."""
    # Single-name placeholders are valid cookie values; the email placeholder
    # contains '@' which we test via the JSON-key shape (cookies in JSON-key
    # shape are explicitly enumerated below).
    if "@" in placeholder:
        pytest.skip("header form doesn't apply to email-shaped placeholder")
    header = f"Cookie: SID={placeholder}"
    ok, leaks = is_clean(header)
    assert ok, leaks


def test_is_clean_accepts_scrubbed_storage_state_value() -> None:
    text = '{"name":"SID","value":"SCRUBBED","domain":".google.com"}'
    ok, leaks = is_clean(text)
    assert ok, leaks


def test_is_clean_accepts_scrubbed_cookie_json_key() -> None:
    text = '{"SID":"SCRUBBED","__Secure-1PSID":"SCRUBBED"}'
    ok, leaks = is_clean(text)
    assert ok, leaks


def test_is_clean_accepts_scrubbed_email_placeholder() -> None:
    text = '{"email":"SCRUBBED_EMAIL@example.com"}'
    ok, leaks = is_clean(text)
    assert ok, leaks


# ---------------------------------------------------------------------------
# is_clean — negative: real leaks are detected
# ---------------------------------------------------------------------------


def test_is_clean_flags_real_email() -> None:
    """A real ``@gmail.com`` address survives a missing-scrub pass."""
    ok, leaks = is_clean('{"email":"realname@gmail.com"}')
    assert not ok
    assert any("realname" in leak or "email" in leak.lower() for leak in leaks)


def test_is_clean_flags_sid_starting_with_S_in_header_form() -> None:
    """**I7 closure**: a real SID value starting with ``S`` is detected.

    The legacy bash guard used a ``[^S"]`` character class on the cookie value,
    which exempted any real secret whose first character was ``S``. The new
    registry uses an exact-match :data:`SCRUB_PLACEHOLDERS` allowlist so the
    starting character is irrelevant — anything not in the allowlist is a leak.
    """
    text = "Set-Cookie: SID=S_REAL_LEAKED_TOKEN; Path=/"
    ok, leaks = is_clean(text)
    assert not ok, "S-prefixed real cookie value should be flagged"
    assert any("SID" in leak for leak in leaks)


def test_is_clean_flags_sid_starting_with_S_in_storage_state() -> None:
    """Same I7 hole in Playwright ``storage_state.json`` shape."""
    text = '{"name":"SID","value":"S_REAL_VALUE","domain":".google.com"}'
    ok, leaks = is_clean(text)
    assert not ok
    assert any("SID" in leak for leak in leaks)


def test_is_clean_flags_sid_starting_with_S_in_json_key() -> None:
    """Same I7 hole in the JSON-dict-with-cookie-name-as-key shape."""
    text = '{"SAPISID": "S_real_leaked_token_here"}'
    ok, _ = is_clean(text)
    assert not ok


def test_is_clean_flags_short_one_char_cookie_value() -> None:
    """A single-character non-scrubbed leak (``"SID": "x"``) is detected."""
    text = '{"SID": "x"}'
    ok, _ = is_clean(text)
    assert not ok


def test_is_clean_flags_wiz_global_data_unscrubbed_csrf() -> None:
    """``SNlM0e`` left at its real value is flagged as a leak."""
    text = '{"SNlM0e":"AB-some-real-CSRF-token-value-12345"}'
    ok, leaks = is_clean(text)
    assert not ok
    assert any("SNlM0e" in leak or "CSRF" in leak.upper() for leak in leaks)


# ---------------------------------------------------------------------------
# Round-trip: scrub_string(x) is always is_clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leak_input",
    [
        "Cookie: SID=ABCDEF1234567890",
        "Cookie: __Secure-1PSID=REAL_VALUE; Path=/",
        "Set-Cookie: SAPISID=S_REAL_TOKEN; Path=/",
        '{"name":"SID","value":"S_REAL_VALUE"}',
        '{"value":"S_REAL_VALUE","name":"__Secure-1PSID"}',
        # Direct JSON-key cookie shape (round-trip via the rule added to
        # close the validator/sanitizer asymmetry).
        '{"SID": "S_REAL_LEAKED_TOKEN"}',
        '{"SAPISID":"S_real_leaked_token_here"}',
        '{"__Secure-1PSID": "S_REAL_VALUE"}',
        '{"email":"alice@gmail.com"}',
        '{"SNlM0e":"real-csrf-here"}',
        '{"FdrFJe":"real-session-here"}',
        '{"oPEP7c":"alice@gmail.com"}',
        '{"S06Grb":"123456789012345678901"}',
        '{"W3Yyqf":"123456789012345678901"}',
        '{"qDCSke":"123456789012345678901"}',
        '{"B8SWKb":"AIzaSyAREAL_API_KEY_HERE"}',
        '{"VqImj":"AIzaSyAREAL_API_KEY_HERE"}',
        '{"QGcrse":"real-client-id"}',
        '{"iQJtYd":"real-project-id"}',
        "f.sid=REAL_SESSION_TOKEN",
        "at=REAL_CSRF_TOKEN",
    ],
)
def test_scrub_then_is_clean_round_trip(leak_input: str) -> None:
    """Anything :func:`scrub_string` produces must satisfy :func:`is_clean`."""
    scrubbed = scrub_string(leak_input)
    ok, leaks = is_clean(scrubbed)
    assert ok, f"scrubbed output still leaks: {leaks}"


# ---------------------------------------------------------------------------
# Registry ↔ vcr_config sync
# ---------------------------------------------------------------------------


def test_vcr_config_uses_registry_scrub_string() -> None:
    """``vcr_config.scrub_string`` is sourced from ``cassette_patterns``.

    ``vcr_config`` loads ``cassette_patterns`` via ``importlib.util.spec_from_
    file_location`` (a separate module identity from the ``sys.path``-import
    used by the test harness), so we cannot use ``is`` identity. Instead we
    pin both the source-file location and the byte-for-byte source of the
    bound function — if a future refactor reintroduces an inline pattern list
    in ``vcr_config`` and reassigns ``scrub_string`` to a local definition,
    either check fails.
    """
    import inspect

    assert inspect.getfile(vcr_config.scrub_string).endswith("cassette_patterns.py")
    assert inspect.getsource(vcr_config.scrub_string) == inspect.getsource(scrub_string)


def test_vcr_config_has_no_inline_sensitive_patterns() -> None:
    """``vcr_config`` MUST NOT define its own ``SENSITIVE_PATTERNS`` list.

    Audit finding I6: drift between recorder and guard. The registry is the
    single source of truth; this test fails if vcr_config recreates a local
    copy.
    """
    assert not hasattr(vcr_config, "SENSITIVE_PATTERNS")


def test_registry_session_cookies_all_scrubbed_by_vcr_config() -> None:
    """Each :data:`SESSION_COOKIES` name has a working scrubber.

    This is the registry-sync invariant: if either the registry's cookie list
    or ``vcr_config``'s scrubber pipeline drifts so that a declared cookie
    name no longer gets its value scrubbed, this test fails.
    """
    for name in SESSION_COOKIES:
        header = f"Cookie: {name}=REAL_SECRET_TOKEN_HERE"
        scrubbed = vcr_config.scrub_string(header)
        assert "REAL_SECRET_TOKEN_HERE" not in scrubbed, (
            f"{name} declared in SESSION_COOKIES but not scrubbed by vcr_config"
        )
        assert f"{name}=SCRUBBED" in scrubbed


def test_registry_secure_cookies_all_scrubbed_by_vcr_config() -> None:
    """Each :data:`SECURE_COOKIES` name is caught by the umbrella scrubber."""
    for name in SECURE_COOKIES:
        header = f"Cookie: {name}=REAL_SECRET"
        scrubbed = vcr_config.scrub_string(header)
        assert "REAL_SECRET" not in scrubbed
        assert f"{name}=SCRUBBED" in scrubbed


def test_filter_headers_disjoint_from_cookies() -> None:
    """VCR ``filter_headers`` covers HTTP-header-only entries — not cookies.

    Cookies are scrubbed via :func:`scrub_string`, not dropped via
    ``filter_headers``. If a future change moves an SID-family name into
    ``filter_headers`` (which would silently drop the entire ``Cookie``
    header from every cassette and break replay), this assertion catches it.
    """
    cookie_names = set(SESSION_COOKIES) | set(SECURE_COOKIES) | set(HOST_COOKIES)
    filter_headers = set(vcr_config.notebooklm_vcr.filter_headers)
    overlap = cookie_names & filter_headers
    assert not overlap, (
        f"cookie names found in vcr filter_headers (should be scrubbed, not dropped): {overlap}"
    )


# ---------------------------------------------------------------------------
# Bad-cassette regression sanity check (inline payloads so this test does
# not depend on filesystem fixtures)
# ---------------------------------------------------------------------------


def test_bad_cassette_byte_count_payload_with_email_is_flagged() -> None:
    """A synthetic bad-cassette body with a leaked email is flagged."""
    body = '12\n{"u":"alice@gmail.com"}\n'
    ok, _ = is_clean(body)
    assert not ok


def test_bad_cassette_cookie_header_payload_is_flagged() -> None:
    """A synthetic bad-cassette body with a leaked cookie value is flagged."""
    body = "Set-Cookie: SID=S_REAL_LEAK; Path=/\n"
    ok, _ = is_clean(body)
    assert not ok


# ---------------------------------------------------------------------------
# upload + Drive token scrubbing (audit finding I17)
#
# These tests pin down two invariants:
#
# 1. Upload tokens (X-GUploader-UploadID headers, upload_id query params,
#    full upload URLs) and Drive tokens (AONS permission tokens, Drive
#    file IDs inside ``"file_id":`` JSON or ``/drive/v3/files/`` URLs)
#    scrub to their canonical placeholders.
#
# 2. The DRIVE_FILE_ID regex is context-anchored so bare 36-char UUIDs
#    elsewhere (artifact IDs, source IDs, conversation IDs) are NOT
#    scrubbed — those are NotebookLM-internal identifiers and matching
#    them would corrupt cassette replay. The negative regression block
#    is the load-bearing test here.
# ---------------------------------------------------------------------------


def test_upload_id_header_scrubbed() -> None:
    header = "X-GUploader-UploadID: ABPj22qXYZ_-abc123def456"
    scrubbed = scrub_string(header)
    assert scrubbed == "X-GUploader-UploadID: SCRUBBED_UPLOAD_ID"
    assert "ABPj22qXYZ" not in scrubbed


def test_upload_url_full_scrubbed() -> None:
    """A full notebooklm upload URL is replaced with the URL placeholder.

    The whole URL collapses to ``SCRUBBED_UPLOAD_URL`` (rather than leaving
    a half-scrubbed ``upload_id=...`` fragment) so that the standalone
    ``upload_id=`` pattern below cannot re-match the placeholder — which
    would otherwise produce a non-idempotent rewrite.
    """
    url = (
        "https://notebooklm.google.com/upload/_/?authuser=0&upload_id=AJRbA5XZXPNXlxYzAbcdef_-12345"
    )
    scrubbed = scrub_string(url)
    assert scrubbed == "SCRUBBED_UPLOAD_URL"
    assert "AJRbA5XZXPNXlx" not in scrubbed


def test_upload_id_query_param_scrubbed() -> None:
    """A bare ``upload_id=...`` query param outside the full URL is scrubbed."""
    text = "POST /resume?upload_id=AJRbA5XZ_-1234567890 HTTP/1.1"
    scrubbed = scrub_string(text)
    assert "AJRbA5XZ_-1234567890" not in scrubbed
    assert "upload_id=SCRUBBED_UPLOAD_ID" in scrubbed


def test_drive_aons_token_scrubbed() -> None:
    """A real-world Drive AONS token (50+ chars) is scrubbed."""
    aons = "AONSffzuRealTokenValueWithLotsOfCharsAndDigits1234567890"
    scrubbed = scrub_string(aons)
    assert scrubbed == "SCRUBBED_AONS"


def test_drive_aons_short_prefix_not_matched() -> None:
    """The literal string ``AONS`` (and short tails) is NOT scrubbed.

    The 20-char tail threshold avoids matching incidental ``AONS`` mentions
    in code, comments, or short identifiers.
    """
    assert scrub_string("AONS") == "AONS"
    assert scrub_string("AONSshort") == "AONSshort"


def test_drive_file_id_in_json_key_scrubbed() -> None:
    """File ID inside a ``"file_id": "..."`` JSON key is scrubbed."""
    text = '{"file_id": "1NFoP9ORcSIk_dTXwMhZWRHfX6JvqYdRqmzYp8LqLi-s"}'
    scrubbed = scrub_string(text)
    assert "1NFoP9ORcSIk" not in scrubbed
    assert '"file_id": "SCRUBBED_DRIVE_FILE_ID"' in scrubbed


def test_drive_file_id_in_drive_url_scrubbed() -> None:
    """File ID inside a ``/drive/v3/files/<id>`` URL is scrubbed."""
    url = "https://www.googleapis.com/drive/v3/files/1A2B3C4D5E6F7G8H9I0J1K2L3M4N5O6P7Q8R"
    scrubbed = scrub_string(url)
    assert "1A2B3C4D5E6F7G8H9I" not in scrubbed
    assert "/drive/v3/files/SCRUBBED_DRIVE_FILE_ID" in scrubbed


def test_drive_file_id_negative_regression_non_drive_ids_not_scrubbed() -> None:
    """Bare 36-char UUIDs outside Drive contexts must NOT be scrubbed.

    This is the load-bearing test for Drive-file-ID scrubbing. The codebase
    emits many ``[A-Za-z0-9-]`` identifiers that LOOK like Drive file IDs but are
    NotebookLM-internal (artifact IDs, conversation IDs, source IDs). A
    naive ``[A-Za-z0-9_-]{33,44}`` pattern would corrupt cassette replay by
    mangling them; DRIVE_FILE_ID is intentionally anchored to ``"file_id":``
    JSON keys and ``/drive/v3/files/`` URLs only.
    """
    non_drive_ids = [
        "71669a91-d5f0-4298-913e-9193178ec62c",  # notebook ID
        "62e5c8db-3dd2-407c-8d19-32ae4ae799db",  # artifact ID
        "f66923f0-1df4-4ffe-9822-3ed63c558b1c",  # conversation ID
        "953b658a-579b-4b3c-b280-43b3781babf3",  # source ID
        "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e",  # share ID
        "167481cd-23a3-4331-9a45-c8948900bf91",  # another notebook ID
    ]
    for non_drive_id in non_drive_ids:
        wrappers = [
            f'{{"artifact_id": "{non_drive_id}"}}',
            f'{{"conversation_id": "{non_drive_id}"}}',
            f'{{"source_id": "{non_drive_id}"}}',
            f"/v1/notebooks/{non_drive_id}/sources",
            f"/v1/artifacts/{non_drive_id}",
        ]
        for wrapped in wrappers:
            assert scrub_string(wrapped) == wrapped, (
                f"DRIVE_FILE_ID false-positive on non-Drive identifier: {wrapped!r}"
            )


def test_upload_drive_scrubbing_is_idempotent() -> None:
    """Scrubbing an already-scrubbed string is a no-op for upload/Drive patterns."""
    inputs = [
        "X-GUploader-UploadID: ABPj22qXYZ_-abc123",
        "https://notebooklm.google.com/upload/_/?upload_id=AJRbA5XZ_-12345",
        "POST /resume?upload_id=AJRbA5XZ_-1234567890 HTTP/1.1",
        "AONSffzuTokenWithEnoughCharsToMatch12345",
        '{"file_id": "1NFoP9ORcSIk_dTXwMhZWRHfX6JvqYdRqmzYp8LqLi-s"}',
        "/drive/v3/files/1A2B3C4D5E6F7G8H9I0J1K2L3M4N5O6P7Q8R",
    ]
    for raw in inputs:
        once = scrub_string(raw)
        twice = scrub_string(once)
        assert once == twice, f"Scrubbing not idempotent for: {raw!r}"


def test_is_clean_flags_unscrubbed_upload_id() -> None:
    """is_clean must catch an unscrubbed upload token."""
    dirty = "X-GUploader-UploadID: ABPj22qXYZ_-abc123def456"
    ok, leaks = is_clean(dirty)
    assert not ok
    assert any("ABPj22qXYZ" in leak for leak in leaks)


def test_is_clean_flags_unscrubbed_aons() -> None:
    """is_clean must catch an unscrubbed Drive AONS token."""
    dirty = "value=AONSffzuRealTokenValueWithLotsOfChars1234567890"
    ok, leaks = is_clean(dirty)
    assert not ok
    assert any("AONS" in leak for leak in leaks)


def test_is_clean_flags_unscrubbed_drive_file_id_in_json() -> None:
    """is_clean must catch an unscrubbed Drive file ID inside a JSON key."""
    dirty = '{"file_id": "1NFoP9ORcSIk_dTXwMhZWRHfX6JvqYdRqmzYp8LqLi-s"}'
    ok, leaks = is_clean(dirty)
    assert not ok
    assert any("1NFoP9ORcSIk" in leak for leak in leaks)


def test_is_clean_flags_unscrubbed_upload_url() -> None:
    """is_clean must catch an unscrubbed full upload URL."""
    dirty = "https://notebooklm.google.com/upload/_/?upload_id=AJRbA5XZ_-12345"
    ok, leaks = is_clean(dirty)
    assert not ok


def test_is_clean_recognizes_upload_drive_placeholders() -> None:
    """The four upload + Drive placeholders register as clean."""
    for placeholder in [
        "X-GUploader-UploadID: SCRUBBED_UPLOAD_ID",
        "upload_id=SCRUBBED_UPLOAD_ID",
        "SCRUBBED_UPLOAD_URL",
        "SCRUBBED_AONS",
        '{"file_id": "SCRUBBED_DRIVE_FILE_ID"}',
        "/drive/v3/files/SCRUBBED_DRIVE_FILE_ID",
    ]:
        ok, leaks = is_clean(placeholder)
        assert ok, f"Placeholder form wrongly flagged as leak: {placeholder!r} -> {leaks}"


# ---------------------------------------------------------------------------
# display-name + avatar scrub patterns (audit C4 + /ogw/ group)
#
# These tests pin down four invariants:
#
# 1. Escaped JSON display-name literals (``\"First Last\"`` inside a double-
#    encoded WRB payload) scrub to ``\"SCRUBBED_NAME\"``.
# 2. ``lh3.googleusercontent.com/(?:a|ogw)/<token>`` avatar URLs scrub to
#    ``SCRUBBED_AVATAR_URL`` for BOTH path forms and BOTH sizing suffix
#    variants (``=s512`` and ``=s32-c-mo``).
# 3. Legitimate two-Capitalized-word strings (font families like Google Sans,
#    UI titles like Account Information, test-corpus artifact / notebook
#    titles, AND bare-quoted source titles like ``"Source Title"``) are NEVER
#    scrubbed. This is the LOAD-BEARING test: a broad
#    ``>[A-Z][a-z]+\\s[A-Z][a-z]+<`` regex without the false-positive
#    allowlist + escape anchoring would corrupt cassette replay. The audit
#    explicitly flagged this as the failure mode to prevent.
# 4. Scrubbing is idempotent on already-scrubbed input; ``is_clean`` flags
#    raw leaks and accepts the canonical placeholders as clean.
# ---------------------------------------------------------------------------


# Helper: a literal backslash-quote pair as it would appear inside a JSON-
# stringified WRB payload. Using a tiny helper keeps the test bodies readable
# (without it every assertion would be a forest of ``\\\\"``).
def _esc(s: str) -> str:
    """Wrap ``s`` in the cassette's escaped-quote form ``\\"...\\"``."""
    return f'\\"{s}\\"'


def test_display_name_escaped_literal_scrubbed() -> None:
    """A real escaped display-name literal scrubs to the SCRUBBED_NAME sentinel."""
    raw = _esc("Alice Smith")
    scrubbed = scrub_string(raw)
    assert scrubbed == _esc("SCRUBBED_NAME")
    assert "Alice Smith" not in scrubbed


def test_display_name_three_word_escaped_literal_scrubbed() -> None:
    """Three-word names (e.g. middle name present) are also scrubbed."""
    raw = _esc("Mary Ann Smith")
    scrubbed = scrub_string(raw)
    assert scrubbed == _esc("SCRUBBED_NAME")
    assert "Mary Ann Smith" not in scrubbed


def test_display_name_inside_wrb_payload_scrubbed() -> None:
    """The exact shape from sharing_get_status.yaml is scrubbed correctly.

    This is the realistic payload form: a stringified JSON list inside a
    WRB envelope, with the display name as a positional list element rather
    than under a structured ``"displayName": "..."`` key. The A4 patterns
    do NOT fire on this shape — that's the gap A6a closes.
    """
    payload = (
        '[[[\\"SCRUBBED_EMAIL@example.com\\",1,[],'
        '[\\"People Conf\\",\\"https://lh3.googleusercontent.com/a/ACg8ocXYZabc=s512\\"]]]'
    )
    scrubbed = scrub_string(payload)
    assert "People Conf" not in scrubbed
    assert "ACg8ocXYZabc" not in scrubbed
    assert _esc("SCRUBBED_NAME") in scrubbed
    assert "SCRUBBED_AVATAR_URL" in scrubbed


def test_avatar_url_a_path_scrubbed() -> None:
    """The ``/a/`` avatar URL form is scrubbed to SCRUBBED_AVATAR_URL."""
    url = "https://lh3.googleusercontent.com/a/ACg8ocImrMoQR5mQnUHZzuc6Tat88aWfSwMre0nCoCanft5bLuZ3dTV0=s512"
    scrubbed = scrub_string(url)
    assert scrubbed == "SCRUBBED_AVATAR_URL"
    assert "ACg8oc" not in scrubbed


def test_avatar_url_ogw_path_scrubbed() -> None:
    """The ``/ogw/`` avatar URL form is scrubbed to SCRUBBED_AVATAR_URL."""
    url = "https://lh3.googleusercontent.com/ogw/AF2bZyi16LQ_0jOcB_3NwTmyCfSFpN74FaCfwF0mWwtxF--cwSQ=s192-c-mo"
    scrubbed = scrub_string(url)
    assert scrubbed == "SCRUBBED_AVATAR_URL"
    assert "AF2bZyi" not in scrubbed


def test_avatar_url_sizing_suffix_variants_scrubbed() -> None:
    """Both ``=s512`` and ``=s32-c-mo`` style sizing suffixes are captured."""
    for tail in ("=s32", "=s64-c-mo", "=s83-c-mo", "=s512"):
        url = f"https://lh3.googleusercontent.com/ogw/AF2bZyABC123_-xyz{tail}"
        scrubbed = scrub_string(url)
        assert scrubbed == "SCRUBBED_AVATAR_URL", (
            f"Sizing suffix {tail!r} not fully scrubbed: {scrubbed!r}"
        )


def test_display_name_false_positive_allowlist_preserved() -> None:
    """Every entry in DISPLAY_NAME_FALSE_POSITIVES is preserved verbatim.

    This is part of the load-bearing negative regression: font family
    strings (``Google Sans``), UI page titles (``Account Information``),
    and artifact / notebook titles from the test corpus must never be
    scrubbed even though they match the two-Capitalized-word shape.
    """
    for fp in DISPLAY_NAME_FALSE_POSITIVES:
        raw = _esc(fp)
        scrubbed = scrub_string(raw)
        assert scrubbed == raw, (
            f"False-positive allowlist breach: {fp!r} got scrubbed to {scrubbed!r}"
        )


def test_display_name_negative_regression_bare_source_titles_preserved() -> None:
    """Bare-quoted two-Capitalized-word source titles must NOT be scrubbed.

    THIS IS THE LOAD-BEARING TEST. The audit explicitly warned that a broad
    ``>[A-Z][a-z]+\\s[A-Z][a-z]+<`` regex would corrupt source-rename and
    artifact-list cassettes. The A6a scrubber anchors on the escape-quote
    form ``\\"...\\"``, so bare-quoted ``"Source Title"`` (which is how
    artifact / notebook / source titles actually appear in cassettes) is
    safe — both the escape anchoring AND the false-positive allowlist
    cooperate to prevent corruption.
    """
    bare_titles = [
        '"Source Title"',
        '"Apple Pie"',
        '"Recipe Book"',
        '"Quantum Physics"',
        '"Machine Learning"',
        '"Web Development"',
        '"Data Science"',
        '"Climate Change"',
    ]
    for bare in bare_titles:
        scrubbed = scrub_string(bare)
        assert scrubbed == bare, (
            f"Bare-quoted source title wrongly scrubbed: {bare!r} -> {scrubbed!r}"
        )


def test_display_name_negative_regression_escaped_titles_in_allowlist_preserved() -> None:
    """Escaped artifact/notebook titles from the test corpus are preserved.

    A cassette body containing ``\\"Agent Quiz\\"`` (e.g. an artifact title)
    must NOT be scrubbed — the false-positive allowlist protects it. Without
    this protection, the escape-anchored regex would clobber the title and
    cassette replay would fail signature matching against the recorded
    request bodies.
    """
    titles = [
        "Agent Quiz",
        "Agent Flashcards",
        "Agent Development Tutorials",
        "Slide Deck",
        "Tool Use Loop",
        "Claude Code",
    ]
    for title in titles:
        raw = _esc(title)
        scrubbed = scrub_string(raw)
        assert scrubbed == raw, (
            f"Test-corpus title wrongly scrubbed: {title!r} ({raw!r}) -> {scrubbed!r}"
        )


def test_display_name_and_avatar_scrubbing_is_idempotent() -> None:
    """Scrubbing an already-scrubbed string is a no-op for display-name/avatar patterns."""
    inputs = [
        _esc("Alice Smith"),
        _esc("People Conf"),
        "https://lh3.googleusercontent.com/a/ACg8ocXYZ=s512",
        "https://lh3.googleusercontent.com/ogw/AF2bZy_-cwSQ=s32-c-mo",
        # Realistic combined payload.
        f'[[[\\"SCRUBBED_EMAIL@example.com\\",1,[],[{_esc("People Conf")},'
        f'\\"https://lh3.googleusercontent.com/a/ACg8ocXYZ=s512\\"]]]',
    ]
    for raw in inputs:
        once = scrub_string(raw)
        twice = scrub_string(once)
        assert once == twice, f"Scrubbing not idempotent for: {raw!r}"


def test_is_clean_flags_unscrubbed_escaped_display_name() -> None:
    """is_clean must catch an unscrubbed escaped display-name literal."""
    dirty = _esc("Alice Smith")
    ok, leaks = is_clean(dirty)
    assert not ok
    assert any("Alice Smith" in leak for leak in leaks)


def test_is_clean_ignores_false_positive_escaped_literals() -> None:
    """is_clean must NOT flag false-positive allowlist entries.

    The detector consults DISPLAY_NAME_FALSE_POSITIVES at the call site,
    so escaped ``\\"Google Sans\\"`` and friends don't surface as leaks
    even though they match the two-Capitalized-word regex shape.
    """
    for fp in DISPLAY_NAME_FALSE_POSITIVES:
        ok, leaks = is_clean(_esc(fp))
        # Filter to only display-name leaks; the email/cookie detectors
        # don't fire on these inputs but be explicit anyway.
        display_leaks = [leak for leak in leaks if "escaped display name" in leak]
        assert not display_leaks, (
            f"False-positive {fp!r} wrongly flagged as display-name leak: {display_leaks}"
        )


def test_is_clean_flags_unscrubbed_avatar_url() -> None:
    """is_clean must catch an unscrubbed avatar URL (both /a/ and /ogw/)."""
    for url in (
        "https://lh3.googleusercontent.com/a/ACg8ocXYZabc=s512",
        "https://lh3.googleusercontent.com/ogw/AF2bZy_-cwSQ=s32-c-mo",
    ):
        ok, leaks = is_clean(url)
        assert not ok
        assert any("avatar URL" in leak for leak in leaks)


def test_is_clean_recognizes_display_name_avatar_placeholders() -> None:
    """The display-name + avatar placeholders register as clean."""
    placeholders = [
        "SCRUBBED_AVATAR_URL",
        _esc("SCRUBBED_NAME"),
        # Realistic combined: an already-scrubbed sharing payload.
        f'[[[\\"SCRUBBED_EMAIL@example.com\\",1,[],[{_esc("SCRUBBED_NAME")},'
        f'\\"SCRUBBED_AVATAR_URL\\"]]]',
    ]
    for placeholder in placeholders:
        ok, leaks = is_clean(placeholder)
        assert ok, f"Placeholder form wrongly flagged as leak: {placeholder!r} -> {leaks}"


def test_display_name_false_positives_mirror_shape_lint() -> None:
    """The scrub-registry allowlist must stay in sync with the shape-lint allowlist.

    ``tests/unit/test_cassette_shapes.py`` carries the same set under a
    slightly different name (``DISPLAY_NAME_FALSE_POSITIVES``), with each
    entry wrapped in the cassette's escape-quote form. If the two drift,
    a real cassette could pass shape-lint but trip the scrub detector (or
    vice versa) — this test forces both lists to be updated together.
    """
    from test_cassette_shapes import DISPLAY_NAME_FALSE_POSITIVES as SHAPE_LINT_FPS

    # Shape-lint stores entries as ``\"Name\"``; the scrub registry stores
    # bare names. Strip the escape wrapping to compare apples-to-apples.
    shape_lint_bare = frozenset(
        entry.removeprefix('\\"').removesuffix('\\"') for entry in SHAPE_LINT_FPS
    )
    assert shape_lint_bare == DISPLAY_NAME_FALSE_POSITIVES, (
        "Display-name false-positive allowlist drifted from shape-lint allowlist; "
        "update BOTH tests/cassette_patterns.py and tests/unit/test_cassette_shapes.py"
    )
