"""Shared cassette leak-pattern helpers for the cassette-shape lint.

This is a non-test helper module: its name does not match pytest's
``test_*.py`` collection glob (the ``_`` prefix also marks it private), so
pytest never collects it as a test. It holds the leak-pattern detectors and the
display-name false-positive allowlist that BOTH the gate test
(:mod:`tests._guardrails.test_cassette_shapes`) and the registry-sync test
(:mod:`tests.unit.test_cassette_patterns`) reference, so the registry-sync test
no longer imports from another *test* module.

The canonical scrub registry lives in :mod:`tests.cassette_patterns`; the
detectors here are the minimal cassette-text leak patterns kept alongside the
shape lint (assertion D in ``test_cassette_shapes.py``).
"""

from __future__ import annotations

import re

# Escaped JSON display name: \"Two Capitalized Words\" inside a quoted JSON
# string. Anchored on the escape `\"` so we don't fire on legitimate
# capitalized prose appearing in plain text. Hyphenated tokens are *not*
# matched (to skip HTTP header names like `Content-Type` and font families
# like `Google-Sans-Text`). The broader scrub registry tightens this further
# with an explicit false-positive allowlist before replacing escaped
# display-name literals.
LEAK_DISPLAY_NAME = re.compile(r'\\"(?:[A-Z][a-z]+)(?: [A-Z][a-z]+)+\\"')
# Google account shell display-name shapes not covered by the escaped JSON
# detector: the CONFIG row is anchored to the scrubbed avatar, and visible
# profile-menu rows are anchored to the adjacent scrubbed account email.
LEAK_GBAR_DISPLAY_NAME = re.compile(
    r'(?:\\"|")(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)(?:\\"|"),'
    r'(?:\\"|")SCRUBBED_AVATAR_URL(?:\\"|")'
)
LEAK_ACCOUNT_HTML_DISPLAY_NAME = re.compile(
    r"<div class=[^>]+>(?P<name>[^<>]+)</div>"
    r"<div(?: class=[^>]+)?>SCRUBBED_EMAIL@example\.com</div>"
)
# Durable opaque Google account identifier in the account-shell CONFIG row.
# The quote token accepts raw HTML quotes and YAML's backslash-escaped form.
_ACCOUNT_SHELL_QUOTE = r'(?:\\"|")'
LEAK_GBAR_ACCOUNT_ID = re.compile(
    _ACCOUNT_SHELL_QUOTE
    + r"SCRUBBED_EMAIL@example\.com"
    + _ACCOUNT_SHELL_QUOTE
    + r","
    + _ACCOUNT_SHELL_QUOTE
    + _ACCOUNT_SHELL_QUOTE
    + r","
    + _ACCOUNT_SHELL_QUOTE
    + r"(?P<account_id>[A-Za-z0-9_-]+)"
    + _ACCOUNT_SHELL_QUOTE
)
# Two-capitalized-word strings that are legitimate UI / artifact / notebook
# titles produced during E2E test runs — NOT human display-name leaks. Keeping
# this allowlist explicit so future additions are intentional. Anything new
# that matches the regex but is benign goes here with a one-line comment.
DISPLAY_NAME_FALSE_POSITIVES = frozenset(
    {
        # Google Sans family (font-family CSS in HTML responses).
        '\\"Google Sans\\"',
        '\\"Google Sans Text\\"',
        '\\"Google Sans Flex\\"',
        '\\"Google Sans Arabic\\"',
        '\\"Google Sans Japanese\\"',
        '\\"Google Sans Korean\\"',
        '\\"Google Sans Simplified Chinese\\"',
        '\\"Google Sans Traditional Chinese\\"',
        # Icon font-family CSS in HTML responses (mat-icon.luminous-icon rule) —
        # not a notebook title despite the two-Capitalized-word shape.
        '\\"Luminous Symbols\\"',
        # Browser user-agent brand surfaced in Sec-CH-UA HTML responses.
        '\\"Microsoft Edge\\"',
        # Account UI page title (not a person's name).
        '\\"Account Information\\"',
        # Product branding in the page shell (post-rebrand; see ADR/#1973 notes).
        '\\"Gemini Notebook\\"',
        # Artifact / notebook titles produced by the test corpus.
        '\\"Agent Development Tutorials\\"',
        '\\"Agent Flashcards\\"',
        '\\"Agent Quiz\\"',
        '\\"Slide Deck\\"',
        '\\"Tool Use Loop\\"',
        '\\"Claude Code\\"',
    }
)
# lh3.googleusercontent.com avatar URLs — both /a/ and /ogw/ prefixes.
LEAK_AVATAR_URL = re.compile(r"https?://lh3\.googleusercontent\.com/(?:a|ogw)/[A-Za-z0-9_\-=]+")
# Literal IP that the audit caught leaking in example_httpbin_*.yaml.
LEAK_HTTPBIN_IP = re.compile(r"\b108\.5\.149\.175\b")


def _find_leaks(text: str) -> list[str]:
    """Return human-readable leak descriptors found in `text`."""
    leaks: list[str] = []
    for m in LEAK_DISPLAY_NAME.finditer(text):
        if m.group(0) in DISPLAY_NAME_FALSE_POSITIVES:
            continue
        leaks.append(f"escaped display-name literal {m.group(0)!r}")
        break  # one is enough; the message is the same
    for label, regex in (
        ("gbar display name", LEAK_GBAR_DISPLAY_NAME),
        ("account-menu display name", LEAK_ACCOUNT_HTML_DISPLAY_NAME),
    ):
        if m := regex.search(text):
            value = " ".join(m.group("name").split())
            if value != "SCRUBBED_NAME":
                leaks.append(f"{label} {value!r}")
    if m := LEAK_GBAR_ACCOUNT_ID.search(text):
        value = m.group("account_id")
        if value != "SCRUBBED_ACCOUNT_ID":
            leaks.append(f"gbar account ID {value!r}")
    if m := LEAK_AVATAR_URL.search(text):
        leaks.append(f"avatar URL {m.group(0)!r}")
    if m := LEAK_HTTPBIN_IP.search(text):
        leaks.append(f"httpbin IP {m.group(0)!r}")
    return leaks
