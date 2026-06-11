"""``notebooklm-server`` entry point — run the single-tenant REST server.

A local-first HTTP server. A bind guard refuses any non-loopback ``--host``
unless ``NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1`` is set, so the server is never
accidentally exposed to the network; and it refuses to start without a configured
bearer token (``NOTEBOOKLM_SERVER_TOKEN``) — a credential-fronting server must
never run tokenless (fail closed).

Configuration comes from ``NOTEBOOKLM_SERVER_*`` env vars as argparse defaults
(server-specific env stays out of the shared ``_env.py``). This module imports NO
``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys

from ._auth import SERVER_TOKEN_ENV, get_configured_token
from .app import create_app

__all__ = ["main"]

#: Env var that opts a deployment into binding to a non-loopback interface.
ALLOW_EXTERNAL_BIND_ENV = "NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND"

#: Hostnames always treated as loopback even though they are not numeric IP
#: literals. An empty / whitespace host is intentionally NOT here — it must be
#: refused (binding to "" listens on all interfaces).
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def _configure_logging(level: str) -> None:
    """Configure root logging at ``level`` on stderr."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` resolves to a loopback interface."""
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_bind_allowed(host: str, *, allow_external: bool) -> None:
    """Refuse to bind to a non-loopback host unless explicitly opted in.

    An empty / whitespace-only ``host`` is a HARD refusal (fail closed) even with
    ``allow_external`` — binding to "" listens on all interfaces.

    Raises:
        SystemExit: ``host`` is empty/whitespace, or is not loopback and
            ``allow_external`` is ``False``.
    """
    if not host.strip():
        raise SystemExit(
            "Refusing to bind the REST server to an empty host (this would expose "
            "it on all interfaces). Pass an explicit loopback host such as 127.0.0.1."
        )
    if _is_loopback(host) or allow_external:
        return
    raise SystemExit(
        f"Refusing to bind the REST server to non-loopback host '{host}'. This "
        f"would expose the server to the network. Set {ALLOW_EXTERNAL_BIND_ENV}=1 "
        f"to override (only behind a trusted proxy)."
    )


def _check_token_configured() -> None:
    """Refuse to start without a configured bearer token (fail closed)."""
    if get_configured_token() is None:
        raise SystemExit(
            f"Refusing to start the REST server without a bearer token. Set "
            f"{SERVER_TOKEN_ENV} to a secret value (a credential-fronting server "
            f"must never run tokenless)."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notebooklm-server",
        description="Run the notebooklm-py single-tenant REST server.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("NOTEBOOKLM_SERVER_HOST", "127.0.0.1"),
        help="Bind host (loopback unless NOTEBOOKLM_SERVER_ALLOW_EXTERNAL_BIND=1).",
    )
    parser.add_argument(
        "--port",
        # Kept as a string and converted after parse so a bad
        # NOTEBOOKLM_SERVER_PORT default does not crash the parser before --port
        # can override it.
        default=os.environ.get("NOTEBOOKLM_SERVER_PORT", "8000"),
        help="Bind port (default: 8000).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get(SERVER_TOKEN_ENV),
        help=(
            "Bearer token every request must present (default: "
            f"${SERVER_TOKEN_ENV}). Required — the server refuses to start without it."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("NOTEBOOKLM_LOG_LEVEL", "INFO"),
        help="Logging level on stderr (default: INFO).",
    )
    return parser


def _resolve_port(raw: str) -> int:
    """Convert the (possibly env-derived) ``--port`` string to an int, or fail clean."""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"Invalid port {raw!r}: must be an integer "
            f"(check the --port argument and NOTEBOOKLM_SERVER_PORT)."
        ) from None
    if not 0 <= port <= 65535:
        raise SystemExit(
            f"Invalid port {raw!r}: must be between 0 and 65535 "
            f"(check the --port argument and NOTEBOOKLM_SERVER_PORT)."
        )
    return port


def main(argv: list[str] | None = None) -> None:
    """Parse args, enforce the bind + token guards, and run the server."""
    args = _build_parser().parse_args(argv)
    _configure_logging(args.log_level)

    # The REST server is EXPERIMENTAL — its surface and behavior may change in a
    # minor release. Surface this on every startup so operators aren't surprised.
    logging.getLogger("notebooklm.server").warning(
        "notebooklm-server is EXPERIMENTAL: the /v1 surface and behavior may "
        "change without notice. Pin a version for automation."
    )

    # A --token (or its NOTEBOOKLM_SERVER_TOKEN default) seeds the env the auth
    # dependency reads, so an explicit flag works even when the env was unset.
    if args.token:
        os.environ[SERVER_TOKEN_ENV] = args.token

    _check_token_configured()
    allow_external = os.environ.get(ALLOW_EXTERNAL_BIND_ENV) == "1"
    _check_bind_allowed(args.host, allow_external=allow_external)

    import uvicorn

    app = create_app()
    uvicorn.run(
        app, host=args.host, port=_resolve_port(args.port), log_level=args.log_level.lower()
    )


if __name__ == "__main__":
    main()
