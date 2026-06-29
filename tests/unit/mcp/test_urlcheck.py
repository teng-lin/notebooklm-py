"""Unit tests for the shared bare-https-origin check (``mcp/_urlcheck.py``).

Pins parity with the OAuth base-URL validation it was extracted from: a bare https
origin is accepted; http, a ``/mcp`` suffix, and any path/query/fragment are
rejected with a ``SystemExit`` naming the offending env var.
"""

from __future__ import annotations

import pytest

from notebooklm.mcp._urlcheck import _validate_bare_https_origin

ENV = "NOTEBOOKLM_MCP_PUBLIC_URL"


@pytest.mark.parametrize(
    "url",
    [
        "https://host.example",
        "https://host.example/",  # a trailing slash is fine
        "https://host.example:8443",
    ],
)
def test_accepts_bare_https_origin(url: str) -> None:
    # Must not raise.
    _validate_bare_https_origin(url, ENV)


@pytest.mark.parametrize(
    "url",
    [
        "http://host.example",  # non-https
        "https://",  # no host
        "https://host.example/mcp",  # the connector URL, not a bare origin
        "https://host.example/path",
        "https://host.example?x=1",
        "https://host.example#frag",
        "ftp://host.example",
        # Userinfo is rejected: a bare origin has none, and a misconfigured
        # ``https://real@evil.example`` would otherwise mint links carrying the
        # credential / pointing at the wrong host (security finding, defense-in-depth).
        "https://user@host.example",
        "https://user:pass@host.example",
    ],
)
def test_rejects_non_bare_or_non_https(url: str) -> None:
    with pytest.raises(SystemExit) as exc:
        _validate_bare_https_origin(url, ENV)
    # The env var is named so the operator knows which setting to fix.
    assert ENV in str(exc.value)
