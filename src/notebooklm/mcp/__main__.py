"""``notebooklm-mcp`` entry point — run the MCP server.

Two transports are supported:

* **stdio** (default): the client speaks JSON-RPC over stdin/stdout. stdout must
  carry *pristine* JSON-RPC, so all logging is pinned to **stderr**.
* **http**: a streamable-HTTP server. A bind guard refuses any non-loopback
  ``--host`` unless ``NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1`` is set, so an MCP
  server is never accidentally exposed to the network. A second, fail-closed
  guard refuses to start on a non-loopback bind unless ``NOTEBOOKLM_MCP_TOKEN``
  is set — a network-reachable server fronting a full Google account must require
  a bearer token. The token is **env-only** (never a CLI flag, so it cannot leak
  via ``ps aux``) and is verified by :mod:`._auth`.

The auth profile is bound once at startup via ``--profile`` /
``NOTEBOOKLM_PROFILE``. This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys

from ._auth import MCP_TOKEN_ENV, build_auth_provider, get_configured_token
from .server import create_server

__all__ = ["main"]

#: Env var that opts a deployment into binding the HTTP transport to a
#: non-loopback interface. Off by default — the server is local-first.
ALLOW_EXTERNAL_BIND_ENV = "NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND"

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


def _check_http_token_required(host: str, token: str | None) -> None:
    """Refuse a non-loopback HTTP bind without a bearer token (fail closed).

    Keyed off the effective non-loopback bind — NOT the ``ALLOW_EXTERNAL_BIND``
    flag — so a loopback dev run never needs a token, while any network-reachable
    bind (which fronts a full Google account) must carry one. Mirrors the REST
    server's ``_check_token_configured`` startup guard.

    Raises:
        SystemExit: ``host`` is non-loopback and ``token`` is ``None``.
    """
    if not _is_loopback(host) and token is None:
        raise SystemExit(
            f"Refusing to bind the MCP HTTP transport to non-loopback host "
            f"'{host}' without {MCP_TOKEN_ENV} set. A network-reachable "
            f"MCP server fronts a full Google account and must require a "
            f"bearer token. Set {MCP_TOKEN_ENV} to a strong random value."
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
        # Resolve the token once, BEFORE building the server: the same value
        # gates startup (fail closed on a network bind) and, when present,
        # becomes the per-request bearer verifier.
        token = get_configured_token()
        _check_http_token_required(host, token)
        server = create_server(profile=args.profile, auth=build_auth_provider(token))
        server.run(transport="http", host=host, port=_resolve_port(args.port))
    else:
        # show_banner=False keeps FastMCP's startup banner out of the host's logs
        # (and off stdout — stdio requires uncontaminated JSON-RPC).
        server = create_server(profile=args.profile)
        server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
