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
import logging
import os
import sys
from pathlib import Path

from .._serving import check_bind_allowed, is_loopback
from ._auth import ALLOW_EXTERNAL_BIND_ENV, SERVER_TOKEN_ENV, get_configured_token
from .app import create_app

__all__ = ["main"]

#: Env var pointing at a file whose contents are the bearer token. Preferred over
#: the deprecated ``--token`` flag, whose value is visible to other local users
#: via ``ps``.
SERVER_TOKEN_FILE_ENV = "NOTEBOOKLM_SERVER_TOKEN_FILE"


def _configure_logging(level: str) -> None:
    """Configure root logging at ``level`` on stderr."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


#: Loopback classification + bind guard live in the shared ``notebooklm._serving``
#: (one implementation across both servers; IPv4-mapped-IPv6-aware). These thin
#: aliases/wrappers bind the REST-server-specific label + override env var and
#: preserve the module's helper API (used by tests).
_is_loopback = is_loopback


def _check_bind_allowed(host: str, *, allow_external: bool) -> None:
    """Refuse to bind the REST server to a non-loopback host unless opted in.

    Delegates to :func:`notebooklm._serving.check_bind_allowed`; see it for the
    empty-host fail-closed rule.

    Raises:
        SystemExit: ``host`` is empty/whitespace, or is not loopback and
            ``allow_external`` is ``False``.
    """
    check_bind_allowed(
        host,
        allow_external=allow_external,
        what="the REST server",
        allow_env=ALLOW_EXTERNAL_BIND_ENV,
    )


def _check_token_configured() -> None:
    """Refuse to start without a configured bearer token (fail closed)."""
    if get_configured_token() is None:
        raise SystemExit(
            f"Refusing to start the REST server without a bearer token. Set "
            f"{SERVER_TOKEN_ENV} (or point {SERVER_TOKEN_FILE_ENV} / --token-file at "
            f"a file containing it) — a credential-fronting server must never run "
            f"tokenless."
        )


def _reject_argv_token(token: str | None) -> None:
    """Refuse the deprecated ``--token`` flag (it leaks via ``ps``).

    A bearer token passed on the command line is visible to any other local user
    via the process table, so it is never accepted. The token must come from
    ``NOTEBOOKLM_SERVER_TOKEN`` or a token file instead.
    """
    if token is not None:
        raise SystemExit(
            "The --token flag is deprecated and insecure: a token on the command "
            "line is visible to other local users via `ps`. Set the "
            f"{SERVER_TOKEN_ENV} environment variable, or pass --token-file / set "
            f"{SERVER_TOKEN_FILE_ENV} to a file containing the token."
        )


def _load_token_file(path: str) -> None:
    """Read the bearer token from ``path`` into the environment the auth reads.

    Only the file PATH ever appears on argv / the process table — the secret
    itself does not. A missing/unreadable or empty file fails closed with a clear
    message.
    """
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"Could not read the token file {path!r}: {exc}") from exc
    if not token:
        raise SystemExit(f"The token file {path!r} is empty.")
    os.environ[SERVER_TOKEN_ENV] = token


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
        default=None,
        # DEPRECATED and insecure: a token on argv is visible to other local users
        # via ``ps``. Passing it is refused at startup (see ``main``). Kept only to
        # emit a clear migration error instead of an "unknown flag" one.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get(SERVER_TOKEN_FILE_ENV),
        help=(
            "Path to a file whose contents are the bearer token (default: "
            f"${SERVER_TOKEN_FILE_ENV}). Preferred over the environment for "
            "keeping the secret off the process table. The token must otherwise be "
            f"supplied via ${SERVER_TOKEN_ENV}."
        ),
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("NOTEBOOKLM_PROFILE"),
        help=(
            "Auth profile to bind for this server process (default: "
            "$NOTEBOOKLM_PROFILE, else the active profile)."
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

    # The deprecated --token flag is refused outright (it leaks via `ps`). A
    # --token-file (or its env default) seeds the env the auth dependency reads,
    # keeping the secret off the process table.
    _reject_argv_token(args.token)
    if args.token_file:
        _load_token_file(args.token_file)

    _check_token_configured()
    allow_external = os.environ.get(ALLOW_EXTERNAL_BIND_ENV) == "1"
    # Strip once so the guard and the actual bind agree on the host (a trailing
    # space must not sneak past the guard and reach uvicorn).
    host = args.host.strip()
    _check_bind_allowed(host, allow_external=allow_external)

    import uvicorn

    app = create_app(profile=args.profile)
    uvicorn.run(
        app,
        host=host,
        port=_resolve_port(args.port),
        log_level=args.log_level.lower(),
        # In the default (loopback) mode the auth guard trusts ``request.client.host``
        # as the real socket peer, so uvicorn must NOT rewrite it from a spoofable
        # ``X-Forwarded-For`` header. Uvicorn enables ``--proxy-headers`` by default
        # (with ``forwarded_allow_ips="127.0.0.1"``), which would let a request from
        # a loopback-adjacent proxy override the peer address — pin it OFF so the
        # loopback check is peer-by-construction. Only when a deployment opts into an
        # external bind (behind a trusted reverse proxy, where the loopback guard is
        # already disabled) do we honor forwarded headers.
        proxy_headers=allow_external,
    )


if __name__ == "__main__":
    main()
