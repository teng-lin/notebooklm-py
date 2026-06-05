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
# like `Google-Sans-Text`). The broader scrub registry tightens this
# further by requiring an adjacent JSON-key context.
LEAK_DISPLAY_NAME = re.compile(r'\\"(?:[A-Z][a-z]+)(?: [A-Z][a-z]+)+\\"')
# Two-capitalized-word strings that are legitimate UI / artifact / notebook
# titles produced during E2E test runs — NOT human display-name leaks. Keeping
# this allowlist explicit so future additions are intentional. Anything new
# that matches the regex but is benign goes here with a one-line comment.
DISPLAY_NAME_FALSE_POSITIVES = frozenset(
    {
        # Google Sans family (font-family CSS in HTML responses).
        '\\"Google Sans\\"',
        '\\"Google Sans Text\\"',
        '\\"Google Sans Arabic\\"',
        '\\"Google Sans Japanese\\"',
        '\\"Google Sans Korean\\"',
        '\\"Google Sans Simplified Chinese\\"',
        '\\"Google Sans Traditional Chinese\\"',
        # Browser user-agent brand surfaced in Sec-CH-UA HTML responses.
        '\\"Microsoft Edge\\"',
        # Account UI page title (not a person's name).
        '\\"Account Information\\"',
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
    if m := LEAK_AVATAR_URL.search(text):
        leaks.append(f"avatar URL {m.group(0)!r}")
    if m := LEAK_HTTPBIN_IP.search(text):
        leaks.append(f"httpbin IP {m.group(0)!r}")
    return leaks
