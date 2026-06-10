"""Bearer-token + loopback-Host authentication for the ``/v1`` router.

Every ``/v1`` request must carry a valid ``Authorization: Bearer <token>`` header
matching the configured ``NOTEBOOKLM_SERVER_TOKEN`` (compared in constant time),
and must address the server over a loopback ``Host`` literal. Two distinct
guards:

* **Bearer token (401).** A missing / empty / mismatched token is rejected with
  ``401`` *before* any upstream client call. If no token is configured the server
  refuses to start (fail closed — a credential-fronting server must never run
  tokenless); the startup check lives in :mod:`.__main__`.
* **Loopback Host (403).** Even bound to loopback and behind a token, a
  DNS-rebinding attack lets a malicious web page resolve its own hostname to
  ``127.0.0.1`` and drive the account. Rejecting any ``Host`` that is not a
  loopback literal (``127.0.0.1`` / ``[::1]`` / ``localhost``) closes that hole.

The token and the ``Authorization`` header value are NEVER logged (honor the
#1517/#1518 redaction discipline).

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

from fastapi import HTTPException, Request

__all__ = [
    "SERVER_TOKEN_ENV",
    "get_configured_token",
    "require_auth",
]

#: Env var carrying the bearer token the server validates every request against.
SERVER_TOKEN_ENV = "NOTEBOOKLM_SERVER_TOKEN"

#: Hostnames always treated as loopback even though they are not numeric IP
#: literals. An empty host is intentionally absent — it must be rejected.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})

_BEARER_PREFIX = "bearer "


def get_configured_token() -> str | None:
    """Return the configured server token, or ``None`` when unset/empty.

    Read live from the environment so a test can set it per case. An empty or
    whitespace-only value is treated as *unset* (fail closed).
    """
    token = os.environ.get(SERVER_TOKEN_ENV)
    if token is None:
        return None
    token = token.strip()
    return token or None


def _host_is_loopback(host_header: str) -> bool:
    """Return whether the ``Host`` header addresses a loopback literal.

    Strips an optional ``:port`` suffix. Accepts ``localhost``, an IPv4/IPv6
    loopback literal (``127.0.0.1``, ``::1``), and the bracketed IPv6 form
    (``[::1]``). Anything else (a public DNS name, ``0.0.0.0``, an empty host)
    is rejected.
    """
    host = host_header.strip()
    if not host:
        return False
    # Bracketed IPv6 form: "[::1]" or "[::1]:8000".
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return False
        candidate = host[1:end]
        # Anything after "]" must be empty or a ":port" suffix — reject
        # trailing garbage like "[::1]evil.com".
        rest = host[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
    else:
        # Split off a trailing :port only when there is a single colon (an
        # unbracketed bare IPv6 literal has several and is not a valid Host with
        # a port anyway).
        candidate = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    candidate = candidate.strip()
    # Host hostnames are case-insensitive (RFC 3986/7230).
    if candidate.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _extract_bearer(authorization: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header, or None."""
    if not authorization:
        return None
    if authorization[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    return authorization[len(_BEARER_PREFIX) :].strip() or None


async def require_auth(request: Request) -> None:
    """FastAPI dependency: enforce the loopback-Host + bearer-token gate.

    Raises:
        HTTPException: ``403`` if the ``Host`` is not a loopback literal
            (DNS-rebinding guard, checked first); ``401`` if the bearer token is
            missing/empty/mismatched or no token is configured.
    """
    if not _host_is_loopback(request.headers.get("host", "")):
        raise HTTPException(status_code=403, detail="Host not allowed")

    configured = get_configured_token()
    presented = _extract_bearer(request.headers.get("authorization"))
    # Fail closed when no token is configured (defence-in-depth; startup also
    # refuses). Constant-time compare to avoid leaking the token via timing.
    if configured is None or presented is None or not hmac.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
