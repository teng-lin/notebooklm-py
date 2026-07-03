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
import os

from fastapi import HTTPException, Request

from .._serving import LOOPBACK_HOSTNAMES as _LOOPBACK_HOSTNAMES
from .._serving import addr_is_loopback as _addr_is_loopback

__all__ = [
    "ALLOW_EXTERNAL_BIND_ENV",
    "SERVER_TOKEN_ENV",
    "get_configured_token",
    "require_auth",
]

#: Env var carrying the bearer token the server validates every request against.
SERVER_TOKEN_ENV = "NOTEBOOKLM_SERVER_TOKEN"

#: Env var that opts a deployment into serving non-loopback peers (a public bind
#: behind a trusted proxy). When UNSET (the default), the auth dependency enforces
#: loopback on the real PEER address — not the spoofable ``Host`` header — so a
#: ``--host 0.0.0.0`` bind plus a forged ``Host: 127.0.0.1`` cannot reach ``/v1``.
#: Shared with the launcher's bind guard (:mod:`.__main__`).
ALLOW_EXTERNAL_BIND_ENV = "NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND"

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
    return _addr_is_loopback(candidate)


def _peer_is_loopback(request: Request) -> bool:
    """Return whether the request's PEER address is a loopback interface.

    Reads ``request.client.host`` — the actual transport-level source address,
    which a remote caller cannot spoof (unlike the ``Host`` header). ``None`` when
    the ASGI server did not populate a client address (treated as non-loopback,
    fail closed).
    """
    client = request.client
    if client is None:
        return False
    return _addr_is_loopback(client.host)


def _allow_external_bind() -> bool:
    """Whether the deployment opted into serving non-loopback peers."""
    return os.environ.get(ALLOW_EXTERNAL_BIND_ENV) == "1"


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
        HTTPException: ``403`` if the request originates off-loopback — enforced on
            the unspoofable PEER address (``request.client.host``), with the
            ``Host``-header literal kept as a supplemental DNS-rebinding guard;
            both are skipped when ``NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1``.
            ``401`` if the bearer token is missing/empty/mismatched or no token is
            configured.
    """
    if not _allow_external_bind():
        # Enforce loopback on the real PEER address first (unspoofable). A
        # ``--host 0.0.0.0`` bind plus a forged ``Host: 127.0.0.1`` no longer
        # reaches here. The Host-header check stays as a supplemental
        # DNS-rebinding guard for the loopback-bound case.
        if not _peer_is_loopback(request):
            raise HTTPException(status_code=403, detail="Peer address is not loopback")
        if not _host_is_loopback(request.headers.get("host", "")):
            raise HTTPException(status_code=403, detail="Host not allowed")

    configured = get_configured_token()
    presented = _extract_bearer(request.headers.get("authorization"))
    # Fail closed when no token is configured (defence-in-depth; startup also
    # refuses). Constant-time compare to avoid leaking the token via timing.
    if configured is None or presented is None or not hmac.compare_digest(presented, configured):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
