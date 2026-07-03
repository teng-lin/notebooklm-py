"""Shared, transport-neutral message redaction for wire-bound error text.

Both adapter error projectors (:mod:`notebooklm.mcp._errors` and
:mod:`notebooklm.server._errors`) must scrub the same set of leak shapes before
an exception message reaches a client: the package-wide credential shapes
(bearer tokens / session cookies / Google credentials, via
:func:`notebooklm._logging.scrub_secrets`) **plus** two surfaces the raw SDK
string can carry — the signed ``/files/(dl|ul)/<token>`` side-channel token and
local home-directory paths (the OS username is PII / host disclosure).

Historically these two extra patterns lived only in the MCP projector, so a
home-path in a REST error body leaked ``/home/<user>/…``. Lifting them into this
single neutral chokepoint (per the rev-2 plan's Option B) makes the redaction
identical on both surfaces. It lives at the package root rather than under
``_app/`` because it composes :func:`notebooklm._logging.scrub_secrets`, and the
``_app`` boundary gate (``tests/_guardrails/test_app_boundary.py``) forbids
``_app`` from importing private siblings — but both adapter layers may import it.

This module imports NO ``click`` / ``rich`` / ``cli`` / ``fastmcp``.
"""

from __future__ import annotations

import re

from ._logging import scrub_secrets

__all__ = ["DEFAULT_MAX_MESSAGE", "redact"]

#: Default maximum wire length for a redacted message before it is truncated.
DEFAULT_MAX_MESSAGE = 300

#: A home-directory username token: alphanumerics / underscore with INTERNAL
#: dots/hyphens (``john.doe``, ``web-admin``) but no leading/trailing punctuation,
#: so a trailing ``.``/``:``/``)`` of surrounding prose is never eaten.
_HOME_USER = r"\w+(?:[.-]+\w+)*"

#: Redaction patterns applied AFTER the shared ``scrub_secrets`` pass:
#:
#: 1. **Signed file-transfer URL tokens.** The ``/files/(dl|ul)/<token>``
#:    side-channel (ADR-0024) carries an HMAC token ``b64url(body).b64url(mac)``;
#:    the route prefix is kept as a shape hint and the whole token segment is
#:    dropped. The dot-inclusive class redacts the entire segment regardless of
#:    dot count and stops at ``/``, ``?``, whitespace, or end.
#: 2. **Home-directory paths** (POSIX ``/home|/Users`` and Windows
#:    ``X:\Users\``). A single-word username is redacted anywhere; a ``First
#:    Last`` username (one space) only when followed by a path separator, so the
#:    space cannot swallow following prose across multiple paths. The token
#:    alternation is anchored on disjoint character classes (no overlapping
#:    quantifiers), so there is no catastrophic-backtracking risk. Generic
#:    absolute paths (``/var``/``/tmp``) are intentionally NOT redacted.
_EXTRA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(/files/(?:dl|ul)/)[A-Za-z0-9._-]+"), r"\1***"),
    (re.compile(rf"(/(?:home|Users)/)(?:{_HOME_USER} {_HOME_USER}(?=/)|{_HOME_USER})"), r"\1***"),
    (
        re.compile(
            rf"([A-Za-z]:[\\/]Users[\\/])(?:{_HOME_USER} {_HOME_USER}(?=[\\/])|{_HOME_USER})",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)


def redact(message: object, *, max_length: int = DEFAULT_MAX_MESSAGE) -> str:
    """Scrub secrets + local paths, collapse whitespace, and length-cap ``message``.

    Runs the shared package scrubber (:func:`notebooklm._logging.scrub_secrets`)
    then the :data:`_EXTRA_PATTERNS` (signed ``/files/*`` tokens + home-directory
    paths), THEN collapses whitespace and caps the length. Redaction runs
    **before** the length cap so a secret sitting near the cap can never be
    partially revealed.
    """
    text = scrub_secrets(message)
    for pattern, replacement in _EXTRA_PATTERNS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > max_length:
        text = text[:max_length] + "…"
    return text
