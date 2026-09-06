"""Web-client assembly and wire fixtures for the local HTTP fault server."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from functools import partial
from typing import Any
from unittest.mock import patch

from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._client_assembly import _assemble_client
from notebooklm._client_options import normalize_legacy_client_options
from notebooklm._web.transport.middleware import chain as middleware_chain
from notebooklm._web.transport.middleware.retry import RetryMiddleware
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

from .http import HttpFaultServer

OLD_CSRF = "csrf-generation-1"
OLD_SESSION = "session-generation-1"
NEW_CSRF = "csrf-generation-2"
NEW_SESSION = "session-generation-2"
COOKIE_NAME = "NLM_FAULT_SESSION"
OLD_COOKIE = "cookie-generation-1"
NEW_COOKIE = "cookie-generation-2"


def rpc_response(rpc_id: str, payload: object) -> bytes:
    """Encode one real batchexecute response frame."""
    inner = json.dumps(payload, separators=(",", ":"))
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]], separators=(",", ":"))
    return f")]}}'\n{len(chunk)}\n{chunk}\n".encode()


def notebook_row(notebook_id: str, title: str) -> list[Any]:
    return [
        title,
        None,
        notebook_id,
        "📘",
        None,
        [1, False, True, None, None, [1704067200, 0]],
    ]


def list_response(rpc_id: str, notebooks: list[tuple[str, str]]) -> bytes:
    return rpc_response(
        rpc_id, [[notebook_row(notebook_id, title) for notebook_id, title in notebooks]]
    )


def create_response(rpc_id: str, notebook_id: str, title: str) -> bytes:
    return rpc_response(rpc_id, notebook_row(notebook_id, title))


def homepage_response(*, csrf: str = NEW_CSRF, session: str = NEW_SESSION) -> bytes:
    return (
        "<html><script>window.WIZ_global_data={"
        f'"SNlM0e":"{csrf}","FdrFJe":"{session}"'
        "};</script></html>"
    ).encode()


def synthetic_auth() -> AuthTokens:
    jar = CookieJar((Cookie(COOKIE_NAME, ".google.com", "/", OLD_COOKIE, secure=True),)).to_httpx()
    return AuthTokens(
        cookies={(COOKIE_NAME, ".google.com", "/"): OLD_COOKIE},
        csrf_token=OLD_CSRF,
        session_id=OLD_SESSION,
        cookie_jar=jar,
    )


def build_fault_client(
    server: HttpFaultServer,
    *,
    timeout: float = 0.5,
    rate_limit_max_retries: int = 2,
    server_error_max_retries: int = 2,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> NotebookLMClient:
    """Assemble a production Web graph while varying only private test seams.

    ``refresh_callback`` is intentionally omitted: `_assemble_client`'s sentinel
    selects the real ``WebSessionAuth.refresh_base`` coordinator callback.
    """
    client = NotebookLMClient.__new__(NotebookLMClient)
    options = normalize_legacy_client_options(
        timeout=timeout,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        backend="web",
    )
    # The chain builder does not forward the legacy sleep seam to retries.
    # Supply the sleeper through the middleware's constructor during synchronous
    # assembly, restoring the binding before any concurrent cohort can run.
    construction = (
        patch.object(middleware_chain, "RetryMiddleware", partial(RetryMiddleware, sleep=sleep))
        if sleep is not None
        else nullcontext()
    )
    with construction:
        _assemble_client(
            client,
            auth=synthetic_auth(),
            options=options,
            async_client_factory=server.client_factory,
            sleep=sleep,
            refresh_retry_delay=0.0,
        )
    return client


__all__ = [
    "COOKIE_NAME",
    "NEW_COOKIE",
    "NEW_CSRF",
    "NEW_SESSION",
    "OLD_COOKIE",
    "OLD_CSRF",
    "OLD_SESSION",
    "build_fault_client",
    "create_response",
    "homepage_response",
    "list_response",
    "notebook_row",
    "rpc_response",
]
