"""Bearer-token auth for the remote (HTTP) MCP transport.

The stdio transport is local and unauthenticated (FastMCP skips auth for stdio).
The HTTP transport, once bound to a non-loopback interface, fronts a full-account
Google session and MUST require a bearer token — the fail-closed startup check
lives in :mod:`.__main__`, and this module supplies the verifier FastMCP runs on
every request.

Design — mirror the REST server's `server/_auth.py` posture, NOT
``fastmcp``'s ``StaticTokenVerifier``:

* ``StaticTokenVerifier``'s own source warns "Never use this in production —
  tokens are stored in plain text"; it pulls in ``authlib``/JOSE for what is a
  single string comparison; and its token dict requires a ``client_id`` key with
  no default (``{token: {}}`` raises ``KeyError`` on the first request).
* A ~25-line :class:`~fastmcp.server.auth.auth.TokenVerifier` subclass doing a
  constant-time :func:`hmac.compare_digest` on **SHA-256 digests** is the right
  tool: no extra dependency, a clean 401, and only a non-reversible digest of the
  token is retained (never the cleartext, so a ``vars()``/scope dump can't leak
  it) — honoring the #1517/#1518 redaction discipline, the way the REST server
  protects ``NOTEBOOKLM_SERVER_TOKEN``.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from fastmcp.server.auth import AccessToken, TokenVerifier

__all__ = [
    "MCP_TOKEN_ENV",
    "McpBearerAuthProvider",
    "build_auth_provider",
    "get_configured_token",
]

#: Env var carrying the bearer token the HTTP transport validates every request
#: against. Env-only (never a CLI flag) so the value cannot leak via ``ps aux``.
MCP_TOKEN_ENV = "NOTEBOOKLM_MCP_TOKEN"

#: Synthetic client id stamped on a validated token. Single-tenant: there is one
#: token and one logical client, so this is a constant, not a lookup.
_CLIENT_ID = "notebooklm-mcp"


def get_configured_token() -> str | None:
    """Return the configured MCP bearer token, or ``None`` when unset/empty.

    Read live from the environment. An empty / whitespace-only value is treated
    as *unset* (fail closed) — same semantics as the REST server's token.
    """
    token = os.environ.get(MCP_TOKEN_ENV)
    if token is None:
        return None
    token = token.strip()
    return token or None


class McpBearerAuthProvider(TokenVerifier):
    """Gate the HTTP transport on a single bearer token (constant-time compare).

    FastMCP runs :meth:`verify_token` for every request via its
    ``RequireAuthMiddleware``; a ``None`` return is a 401. The instance holds only
    the **SHA-256 digest** of the configured token, never the cleartext — so
    ``vars(provider)`` / a scope dump cannot surface it — and verification hashes
    the presented token and compares digests in constant time.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        # Store only a digest of the configured token (its raw UTF-8 bytes hashed).
        # The digest is non-reversible and cannot be replayed as a token (verify
        # hashes the *presented* value), so no cleartext copy is retained anywhere.
        self.__digest = hashlib.sha256(token.encode("utf-8")).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        # Starlette latin-1-decodes the Authorization header, so ``token`` here is
        # the latin-1 view of the raw request bytes. ``.encode("latin-1")`` round-
        # trips that back to the EXACT bytes the client sent, which we hash and
        # compare against the configured token's UTF-8-byte digest — so an ASCII
        # token and a UTF-8-encoded non-ASCII token both verify correctly. A header
        # string outside latin-1 (cannot come from Starlette) simply fails to match.
        try:
            presented = token.encode("latin-1")
        except UnicodeEncodeError:
            return None
        if hmac.compare_digest(hashlib.sha256(presented).digest(), self.__digest):
            # Do NOT echo the live token into the AccessToken: FastMCP stores it on
            # the request scope (scope["user"]) and AccessToken's pydantic repr
            # would expose it in any scope/log dump. We never read the field, so
            # stamp an opaque constant instead.
            return AccessToken(token=_CLIENT_ID, client_id=_CLIENT_ID, scopes=[])
        return None

    def __repr__(self) -> str:  # never surface auth material
        return f"{type(self).__name__}(token=<redacted>)"


def build_auth_provider(token: str | None) -> McpBearerAuthProvider | None:
    """Return a provider for a non-empty ``token``, else ``None`` (no auth).

    The caller (``__main__`` on the http path) resolves the token and, when the
    bind is network-reachable, refuses to start without one; this helper only
    maps token→provider so ``create_server`` stays env-free.
    """
    return McpBearerAuthProvider(token) if token else None
