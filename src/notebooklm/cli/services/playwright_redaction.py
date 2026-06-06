"""Subprocess output sanitisation for the Playwright login service.

Captured stderr/stdout from a Playwright subprocess (e.g. the install-failure
path in :mod:`notebooklm.cli.services.playwright_login`) can leak two classes of
noise into the console:
  1. Environment-variable VALUES — Playwright forwards the parent env, so a
     secret (PSIDTS, API tokens, auth-source / SAPISID cookie material)
     interpolated into a traceback lands verbatim in ``result.stderr``.
  2. ANSI control sequences — pip/playwright progress bars + colour codes.
``redact_subprocess_output`` strips both. Env-var redaction is conservative:
empty / single-char / boolean-ish / path-separator constants are skipped to
avoid false positives across normal stderr lines.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from typing import Any

# CSI: ESC '[' parameter-bytes intermediate-bytes final-byte
# OSC: ESC ']' ... (BEL | ESC '\\')
# Plus a catch-all for any remaining two-byte C1 Fe sequence (the
# 0x40-0x5F final-byte range). CSI and OSC are stripped first so this
# only fires on leftovers (PM ``ESC ^``, APC ``ESC _``, ST ``ESC \``,
# etc.).
_ANSI_CSI_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_PATTERN = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
_ANSI_OTHER_PATTERN = re.compile(r"\x1B[@-_]")

# Env-var values we never redact even if they happen to be set (they would
# produce noisy false positives on every line that mentions a path / boolean).
# Single-character values (``/``, ``.``, ``*``, ``0``, ``1``, ``y``, ``n``)
# don't appear here — they are already excluded by ``_REDACTION_MIN_VALUE_LEN``.
_REDACTION_SAFE_VALUES = frozenset(
    {
        "",
        "..",
        "true",
        "false",
        "True",
        "False",
        "TRUE",
        "FALSE",
        "yes",
        "no",
        "on",
        "off",
    }
)

# Skip env values shorter than this — substring matches on 2-char strings
# false-positive across the bytes Playwright prints.
_REDACTION_MIN_VALUE_LEN = 3


def _strip_ansi(text: str) -> str:
    """Remove ANSI CSI / OSC / two-byte escape sequences from ``text``."""
    text = _ANSI_CSI_PATTERN.sub("", text)
    text = _ANSI_OSC_PATTERN.sub("", text)
    text = _ANSI_OTHER_PATTERN.sub("", text)
    return text


def _expand_nested_secret_values(value: str) -> Iterator[str]:
    """Yield ``value`` plus any nested string leaves if it parses as JSON.

    Env values supplied as inline JSON (the auth-source env var being
    the canonical example) carry serialised dicts whose leaf strings
    (cookie tokens, refresh tokens) are the actual secrets. If a
    subprocess re-emits the parsed nested value rather than the whole
    JSON blob, exact-string matching against the original env value
    would miss the leak. Walk JSON objects/arrays here to add every
    leaf string to the redaction candidate set.

    Non-JSON values yield just themselves (and only if they pass the
    caller's length / safe-value filter).
    """
    yield value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return

    stack: list[Any] = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def redact_subprocess_output(text: str, env: Mapping[str, str] | None = None) -> str:
    """Sanitise captured subprocess ``stdout`` / ``stderr`` before printing.

    Runs in two passes (detail in the inline comments + module header):

    1. **Strip ANSI control sequences** FIRST so a secret split by an inert
       reset (``"abc\\x1b[0m123"`` → ``"abc123"``) is reassembled before the
       exact-match redactor runs — otherwise it would miss it.
    2. **Replace each non-trivial env-var value** (plus any JSON-nested leaf
       strings) with ``<redacted>``. The env is snapshotted from ``os.environ``
       unless ``env`` is supplied. Values under
       :data:`_REDACTION_MIN_VALUE_LEN` and the :data:`_REDACTION_SAFE_VALUES`
       constants are skipped; candidates are tried longest-first so a longer
       secret containing a shorter one is redacted as one token.

    Returns the sanitised string; the input is not mutated.
    """
    if not text:
        return text

    # Pass 1: strip ANSI BEFORE redaction so a secret broken up by reset
    # codes (``"abc\x1b[0m123"`` → ``"abc123"``) is reassembled and
    # redactable by the exact-match pass below.
    text = _strip_ansi(text)

    # ``dict(os.environ)`` defends against another thread mutating the process
    # environment mid-iteration (rare).
    source_env: Mapping[str, str] = dict(os.environ) if env is None else env

    # Candidate set: every env value + any JSON-nested leaf strings, plus the
    # ansi-stripped form of each (the env value may carry embedded control
    # bytes; ``text`` is already stripped). Sorted longest-first below so a
    # longer secret is redacted before any shorter prefix.
    candidates: set[str] = set()
    for raw_value in source_env.values():
        if not isinstance(raw_value, str):
            continue
        for nested in _expand_nested_secret_values(raw_value):
            for variant in (nested, _strip_ansi(nested)):
                if (
                    len(variant) >= _REDACTION_MIN_VALUE_LEN
                    and variant not in _REDACTION_SAFE_VALUES
                ):
                    candidates.add(variant)

    for value in sorted(candidates, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "<redacted>")

    return text
