"""``notebooklm-mcp`` entry point — run the MCP server.

Two transports are supported:

* **stdio** (default): the client speaks JSON-RPC over stdin/stdout. stdout must
  carry *pristine* JSON-RPC, so all logging is pinned to **stderr**.
* **http**: a streamable-HTTP server. A bind guard refuses any non-loopback
  ``--host`` unless ``NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1`` is set, so an MCP
  server is never accidentally exposed to the network. A second, fail-closed
  guard refuses to start on a non-loopback bind unless SOME auth is configured —
  a network-reachable server fronting a full Google account must require either a
  ``NOTEBOOKLM_MCP_TOKEN`` bearer (Claude Code/Desktop, verified by :mod:`._auth`)
  or optional self-hosted OAuth (``NOTEBOOKLM_MCP_OAUTH_PASSWORD`` + base URL, for
  claude.ai, served by :mod:`._oauth`). Both coexist via ``MultiAuth``. All secrets
  are **env-only** (never CLI flags, so they cannot leak via ``ps aux``).

The auth profile is bound once at startup via ``--profile`` /
``NOTEBOOKLM_PROFILE``. This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import secrets
import sys

from ._auth import MCP_TOKEN_ENV, build_auth, get_configured_token
from ._filelink import FileLinkSigner, FileTransferConfig
from ._oauth import (
    OAUTH_BASE_URL_ENV,
    OAUTH_PASSWORD_ENV,
    OAuthConfig,
    build_oauth_provider,
    get_oauth_config,
)
from ._urlcheck import _validate_bare_https_origin
from .server import create_server

__all__ = ["main"]

#: Env var that opts a deployment into binding the HTTP transport to a
#: non-loopback interface. Off by default — the server is local-first.
ALLOW_EXTERNAL_BIND_ENV = "NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND"

#: Public https origin claude.ai reaches the tunnel at, used to build the signed
#: file-transfer URLs. Optional — falls back to the OAuth base URL. When neither is
#: set, remote file transfer is simply unavailable (no startup crash).
PUBLIC_URL_ENV = "NOTEBOOKLM_MCP_PUBLIC_URL"

#: Hostnames that are always treated as loopback even though they are not numeric
#: IP literals. An empty / whitespace host is intentionally NOT here — it must be
#: refused (binding to "" listens on all interfaces).
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})

#: Valid resolved transports. An env-derived default is validated against this
#: AFTER parsing (argparse ``choices`` validates explicit CLI args, but not the
#: env-supplied default).
_VALID_TRANSPORTS = frozenset({"stdio", "http"})


def _configure_logging(level: str) -> None:
    """Pin logging to stderr — the stdio transport requires uncontaminated stdout."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` resolves to a loopback interface.

    Hostnames are case-insensitive, so ``LOCALHOST`` is normalized before the
    check; an IP literal (``127.0.0.1`` / ``::1``) is parsed. Anything else
    (a public DNS name, ``0.0.0.0``, ``::``) is NOT loopback — fail closed.
    """
    normalized = host.strip().lower()
    if normalized in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _check_http_bind_allowed(host: str, *, allow_external: bool) -> None:
    """Refuse to bind the HTTP transport to a non-loopback host unless opted in.

    An empty / whitespace-only ``host`` is a HARD refusal (fail closed) even with
    ``allow_external`` — binding to "" listens on all interfaces, and there is no
    legitimate reason to express that as a blank host rather than an explicit
    ``0.0.0.0`` (which still needs the override).

    Raises:
        SystemExit: ``host`` is empty/whitespace, or is not loopback and
            ``allow_external`` is ``False``.
    """
    if not host.strip():
        raise SystemExit(
            "Refusing to bind the MCP HTTP transport to an empty host "
            "(this would expose the server on all interfaces). Pass an explicit "
            "loopback host such as 127.0.0.1."
        )
    if _is_loopback(host) or allow_external:
        return
    raise SystemExit(
        f"Refusing to bind the MCP HTTP transport to non-loopback host '{host}'. "
        f"This would expose the server to the network. Set "
        f"{ALLOW_EXTERNAL_BIND_ENV}=1 to override (only behind a trusted proxy)."
    )


def _check_http_auth_required(host: str, token: str | None, oauth: OAuthConfig | None) -> None:
    """Refuse a non-loopback HTTP bind without SOME auth (fail closed).

    Keyed off the effective non-loopback bind — NOT the ``ALLOW_EXTERNAL_BIND``
    flag — so a loopback dev run never needs auth, while any network-reachable bind
    (which fronts a full Google account) must carry either a bearer token (Claude
    Code/Desktop) or self-hosted OAuth (claude.ai).

    Raises:
        SystemExit: ``host`` is non-loopback and neither a token nor OAuth is set.
    """
    if not _is_loopback(host) and token is None and oauth is None:
        raise SystemExit(
            f"Refusing to bind the MCP HTTP transport to non-loopback host "
            f"'{host}' without authentication. A network-reachable MCP server "
            f"fronts a full Google account and must require auth: set "
            f"{MCP_TOKEN_ENV} (a strong random bearer for Claude Code/Desktop) "
            f"and/or {OAUTH_PASSWORD_ENV} (+ base URL) for OAuth (claude.ai)."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notebooklm-mcp",
        description="Run the notebooklm-py MCP server.",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("NOTEBOOKLM_PROFILE"),
        help="Auth profile to bind for this server process (default: active profile).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("NOTEBOOKLM_MCP_TRANSPORT", "stdio"),
        help="Transport: 'stdio' (default) or loopback 'http'.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("NOTEBOOKLM_MCP_HOST", "127.0.0.1"),
        help="HTTP bind host (http transport only; loopback unless overridden).",
    )
    parser.add_argument(
        "--port",
        # NOT type=int and NOT int(os.environ[...]) at build time: a bad
        # NOTEBOOKLM_MCP_PORT must not crash the parser before CLI args are read
        # (which would make --port unable to override it). Kept as a string and
        # converted after parse with a clear error (see ``_resolve_port``).
        default=os.environ.get("NOTEBOOKLM_MCP_PORT", "9420"),
        help="HTTP bind port (http transport only; default: 9420).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("NOTEBOOKLM_LOG_LEVEL", "INFO"),
        help="Logging level on stderr (default: INFO).",
    )
    return parser


def _build_file_transfer() -> FileTransferConfig | None:
    """Build the remote file-transfer config from env, or ``None`` when unavailable.

    The public base URL is ``NOTEBOOKLM_MCP_PUBLIC_URL`` falling back to
    ``NOTEBOOKLM_MCP_OAUTH_BASE_URL`` (the tunnel URL the OAuth flow already
    requires). When neither is set the capability is simply absent — **no
    SystemExit** (a bearer-only remote deployment that never uses file transfer
    must keep starting). When set, the value is validated as a bare https origin
    (a ``/mcp``-suffixed or non-https URL would mint broken/unsafe links) and the
    signer gets an ephemeral key minted at startup.

    Raises:
        SystemExit: a public URL is set but is not a bare https origin.
    """
    public_url = os.environ.get(PUBLIC_URL_ENV)
    env_name = PUBLIC_URL_ENV
    if not (public_url or "").strip():
        public_url = os.environ.get(OAUTH_BASE_URL_ENV)
        env_name = OAUTH_BASE_URL_ENV
    public_url = (public_url or "").strip()
    if not public_url:
        return None
    _validate_bare_https_origin(public_url, env_name)
    return FileTransferConfig(signer=FileLinkSigner(secrets.token_bytes(32)), base_url=public_url)


def _resolve_port(raw: str) -> int:
    """Convert the (possibly env-derived) ``--port`` string to an int, or fail clean.

    Done after parse so a bad ``NOTEBOOKLM_MCP_PORT`` default does not crash the
    parser build before ``--port`` can override it.
    """
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"Invalid port {raw!r}: must be an integer "
            f"(check the --port argument and NOTEBOOKLM_MCP_PORT)."
        ) from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"Invalid port {port}: must be in 1..65535.")
    return port


def main(argv: list[str] | None = None) -> None:
    """Parse args, enforce the bind guard, and run the server."""
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)

    # argparse ``choices`` validates an explicit --transport, but NOT an
    # env-derived default; validate the resolved value so a bogus
    # NOTEBOOKLM_MCP_TRANSPORT fails loud instead of silently running stdio.
    if args.transport not in _VALID_TRANSPORTS:
        raise SystemExit(
            f"Invalid transport {args.transport!r}: must be one of "
            f"{sorted(_VALID_TRANSPORTS)} (check --transport and "
            f"NOTEBOOKLM_MCP_TRANSPORT)."
        )

    if args.transport == "http":
        # Normalize the host once and use it for the guards AND the bind — the
        # loopback check tolerates surrounding whitespace, so an env value like
        # " 127.0.0.1 " must not pass the guards and then fail at bind time.
        host = args.host.strip()
        allow_external = os.environ.get(ALLOW_EXTERNAL_BIND_ENV) == "1"
        _check_http_bind_allowed(host, allow_external=allow_external)
        # Resolve auth BEFORE building the server, on the http path only, so
        # create_server stays env-free. The bearer (Claude Code/Desktop) and the
        # optional self-hosted OAuth (claude.ai) are both env-driven; get_oauth_config()
        # raises on partial/weak/non-https config (fail closed).
        token = get_configured_token()
        oauth_config = get_oauth_config()
        _check_http_auth_required(host, token, oauth_config)
        oauth = build_oauth_provider(oauth_config) if oauth_config else None
        # Optional remote file transfer: built only here (http path), validated, and
        # absent (None) when no public URL is set — never a startup crash.
        file_transfer = _build_file_transfer()
        server = create_server(
            profile=args.profile,
            auth=build_auth(token, oauth),
            file_transfer=file_transfer,
        )
        server.run(transport="http", host=host, port=_resolve_port(args.port))
    else:
        # show_banner=False keeps FastMCP's startup banner out of the host's logs
        # (and off stdout — stdio requires uncontaminated JSON-RPC).
        server = create_server(profile=args.profile)
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
