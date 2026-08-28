"""U3: error projection from ``classify`` to HTTP status + typed envelope."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from notebooklm import exceptions as exc
from notebooklm.server._errors import _redact, error_response
from notebooklm.server.app import create_app

from .conftest import TEST_TOKEN
from .fakes import FakeClient


class _RaisingNotebooks:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def list(self) -> list[object]:
        raise self._error


def _client_raising(error: BaseException) -> TestClient:
    fake = FakeClient()
    fake.notebooks = _RaisingNotebooks(error)  # type: ignore[assignment]

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield fake

    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    client = TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    )
    client.__enter__()
    return client


@pytest.mark.parametrize(
    ("error", "status", "category"),
    [
        (exc.ClientError("missing", rpc_code=5), 404, "not_found"),
        (exc.ClientError("missing", rpc_code="5"), 404, "not_found"),
        (exc.RateLimitError("slow down"), 429, "rate_limited"),
        (exc.AuthError("expired"), 401, "auth"),
        (exc.ValidationError("bad"), 400, "validation"),
        (exc.MissingDependencyError("markdownify missing"), 500, "dependency"),
        (exc.RPCError("decode failed"), 502, "rpc"),
        (RuntimeError("boom"), 500, "unexpected"),
    ],
)
def test_exception_projects_to_status_and_category(
    error: BaseException, status: int, category: str
) -> None:
    client = _client_raising(error)
    try:
        resp = client.get("/v1/notebooks")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == status
    body = resp.json()
    assert body["error"]["category"] == category


def test_status_5_preserves_the_scrubbed_message() -> None:
    """The 404 body carries the scrubbed account-routing hint (not dropped)."""
    client = _client_raising(exc.ClientError("wrong authuser hint", rpc_code=5))
    try:
        resp = client.get("/v1/notebooks")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404
    assert "wrong authuser hint" in resp.json()["error"]["message"]


def test_status_7_is_not_routed_to_404() -> None:
    """Code 7 (permission-denied) stays a generic RPC → 502, not 404."""
    client = _client_raising(exc.ClientError("denied", rpc_code=7))
    try:
        resp = client.get("/v1/notebooks")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 502
    assert resp.json()["error"]["category"] == "rpc"


def _client_with_real_notebooks(error: BaseException) -> TestClient:
    """REST app whose ``notebooks`` namespace is the **real** ``NotebooksAPI``.

    The tests above inject a bare ``ClientError`` in place of the whole
    namespace, so they project ``classify`` faithfully but never execute
    ``NotebooksAPI.get()``. Only the ``rpc_call`` seam raises here, so the GET
    route runs the real translation from a status-5 rejection to
    ``NotebookNotFoundError`` — the path an actual missing notebook takes.
    """
    from unittest.mock import AsyncMock, MagicMock

    from notebooklm._web.notebooks import WebNotebooksAPI
    from tests._fixtures.fake_core import make_fake_core

    fake = FakeClient()
    core = make_fake_core(rpc_call=AsyncMock(side_effect=error))
    fake.notebooks = WebNotebooksAPI(  # type: ignore[assignment]
        core.rpc_executor, sources_api=MagicMock()
    )

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield fake

    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    client = TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    )
    client.__enter__()
    return client


def test_get_route_status_5_keeps_the_routing_hint_through_the_translation() -> None:
    """GET /v1/notebooks/{id} preserves the hint *after* the typed translation.

    ``server/_errors.py`` documents that the status-5 account-routing hint is
    preserved verbatim in the 404 body. Once ``NotebooksAPI.get()`` converts
    that rejection into ``NotebookNotFoundError``, keeping the promise depends
    on the translation carrying the diagnostic onto the typed error — the
    renderers print ``str(exc)``, never ``__cause__``.
    """
    hint = "commonly an account-routing mismatch"
    client = _client_with_real_notebooks(
        exc.ClientError(f"The server rejected this request (not found). {hint}", rpc_code=5)
    )
    try:
        resp = client.get("/v1/notebooks/nb-missing")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 404
    body = resp.json()["error"]
    assert body["category"] == "not_found"
    assert hint in body["message"], f"routing hint dropped from the 404 body: {body['message']!r}"


def test_get_route_status_7_is_not_reported_as_a_missing_notebook() -> None:
    """A notebook the caller may not read must not project as 404 through GET.

    The decoder routes PERMISSION_DENIED through the same ``ClientError``
    branch as NOT_FOUND, so this pins that the translation did not widen.
    """
    client = _client_with_real_notebooks(exc.ClientError("denied", rpc_code=7))
    try:
        resp = client.get("/v1/notebooks/nb-forbidden")
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 502
    assert resp.json()["error"]["category"] == "rpc"


def _error_body(exc_obj: BaseException) -> dict[str, object]:
    import json

    return json.loads(error_response(exc_obj).body.decode())["error"]


def test_error_body_carries_retriable_flag() -> None:
    # A retriable category (rate-limit) and a non-retriable one (validation) both
    # surface the neutral ``retriable`` flag so an agent client can branch a backoff.
    retriable = error_response(exc.RateLimitError("slow down"))
    assert retriable.status_code == 429
    assert _error_body(exc.RateLimitError("slow down"))["retriable"] is True
    assert _error_body(exc.ValidationError("bad"))["retriable"] is False


@pytest.mark.parametrize(
    ("cause", "status", "category", "retriable"),
    [
        (exc.NetworkError("offline"), 502, "network", True),
        (exc.ServerError("unavailable"), 502, "server", True),
        (exc.AuthError("expired"), 401, "auth", False),
        (exc.RateLimitError("slow down"), 429, "rate_limited", True),
        (exc.ValidationError("rejected file"), 400, "validation", False),
    ],
)
def test_partial_upload_error_preserves_cause_projection(
    cause: Exception, status: int, category: str, retriable: bool
) -> None:
    """``raise_partial_upload_failure()`` attaches ``source_id``/``stage`` directly
    to the real cause rather than wrapping it, so it must project exactly like an
    ordinary instance of its own type.
    """
    cause.source_id = "source-1"  # type: ignore[attr-defined]
    cause.stage = "upload_finalize"  # type: ignore[attr-defined]

    response = error_response(cause)
    body = _error_body(cause)

    assert response.status_code == status
    assert body["category"] == category
    assert body["retriable"] is retriable


def test_error_body_carries_hint_where_present() -> None:
    assert "hint" in _error_body(exc.RateLimitError("slow down"))
    # A category with no hint (RPC) omits the field entirely.
    assert "hint" not in _error_body(exc.RPCError("decode failed"))


def test_not_found_body_carries_near_miss_candidates() -> None:
    """A failed name lookup surfaces near-miss candidates + a 'Did you mean' hint (#1787)."""
    err = exc.NotebookNotFoundError("Scientific")
    err.candidates = [{"id": "37fe5c1d", "title": "Scientific PDF Parsing"}]
    body = _error_body(err)
    assert body["candidates"] == [{"id": "37fe5c1d", "title": "Scientific PDF Parsing"}]
    assert body["hint"].startswith("Did you mean:")


def test_not_found_body_without_candidates_omits_the_field() -> None:
    body = _error_body(exc.NotebookNotFoundError("Scientific"))
    assert "candidates" not in body


def test_http_error_response_enriches_mapped_status() -> None:
    """A status that maps to a neutral ErrorCategory (411 → validation) carries
    retriable + hint, drawn from the shared _app tables."""
    import json

    from notebooklm.server._errors import http_error_response

    body = json.loads(http_error_response(411, "Content-Length required").body.decode())["error"]
    assert body["category"] == "validation"
    assert body["retriable"] is False
    assert "hint" in body


def test_http_error_response_protocol_only_status_omits_enrichment() -> None:
    """An HTTP-protocol-only status (409 conflict) has no neutral category, so it
    carries just category + message — no retriable / hint."""
    import json

    from notebooklm.server._errors import http_error_response

    body = json.loads(http_error_response(409, "already exists").body.decode())["error"]
    assert body["category"] == "conflict"
    assert "retriable" not in body
    assert "hint" not in body


def test_home_directory_path_is_redacted_in_body() -> None:
    # A file/upload error carrying a local home path must not leak the OS username
    # (PII / host disclosure) on the REST surface — the shared redactor masks it.
    resp = error_response(exc.ValidationError("open /home/secretuser/private/x.pdf failed"))
    body = resp.body.decode()
    assert "secretuser" not in body
    assert "/home/***" in body


def test_long_message_is_truncated() -> None:
    long = "x " * 400
    resp = error_response(exc.RPCError(long))
    body = resp.body.decode()
    assert "…" in body
    # The redacted message is capped well under the raw length.
    assert len(_redact(long)) <= 301


def test_route_404_carries_retriable_and_hint(authed_client: object) -> None:
    """A hand-raised HTTPException (in-route 404) carries the SAME retriable + hint
    enrichment as a classified library error, not just {category, message}."""
    from fastapi.testclient import TestClient

    assert isinstance(authed_client, TestClient)
    resp = authed_client.get("/v1/notebooks/nb-1/sources/nope/content")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["category"] == "not_found"
    assert err["retriable"] is False
    assert "hint" in err and isinstance(err["hint"], str)


def test_route_422_carries_retriable_and_hint(authed_client: object) -> None:
    """A request-body 422 (RequestValidationError → hand-raised path) is enriched."""
    from fastapi.testclient import TestClient

    assert isinstance(authed_client, TestClient)
    resp = authed_client.post("/v1/notebooks/nb-1/chat", json={})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["category"] == "validation"
    assert err["retriable"] is False
    assert "hint" in err


def test_auth_401_carries_retriable_and_hint(app: object) -> None:
    """The auth dependency's 401 (a hand-raised HTTPException) is enriched too."""
    from fastapi.testclient import TestClient

    headers = {"Authorization": "Bearer wrong-token", "Host": "127.0.0.1"}
    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        resp = client.get("/v1/notebooks")
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["category"] == "auth"
    assert err["retriable"] is False
    assert "hint" in err


def test_request_validation_message_has_no_source_paths(authed_client: object) -> None:
    """A malformed body → 422 envelope with a compact field summary, NOT
    ``str(exc)`` (which embeds server file paths / frame info under pydantic v2)."""
    from fastapi.testclient import TestClient

    assert isinstance(authed_client, TestClient)
    resp = authed_client.post("/v1/notebooks/nb-1/chat", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["category"] == "validation"
    message = body["error"]["message"]
    # The missing field is named, but no server path / source file leaks.
    assert "question" in message
    assert ".py" not in message and "/home/" not in message and 'File "' not in message


def test_unconfirmed_create_is_surfaced_in_the_rest_body() -> None:
    """REST parity with the MCP projection (#2220).

    Forced to RPC (HTTP 502), whose category hint is ``None``, so without the
    override the caller gets a bare message and ``retriable: false`` with
    nothing indicating a source may already exist.
    """
    from notebooklm._app.errors import UNCONFIRMED_HINT
    from notebooklm._idempotency import mark_unconfirmed
    from notebooklm.server._errors import error_item

    body = error_item(mark_unconfirmed(exc.NetworkError("connection reset")))

    assert body["unconfirmed"] is True
    assert body["retriable"] is False
    assert body["hint"] == UNCONFIRMED_HINT
    assert "unconfirmed" not in error_item(exc.NetworkError("connection reset"))
