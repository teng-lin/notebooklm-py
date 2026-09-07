"""Safety checks for the test-only logical-host socket router."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import tests._fault_server.web_scenarios as web_scenarios
from tests._fault_server.common import ScenarioFailure, ScenarioResult
from tests._fault_server.http import HttpFaultServer, LogicalHostTransport, Reply, Route, Stall


def test_cleanup_records_every_failed_server_invariant(monkeypatch: pytest.MonkeyPatch) -> None:
    server = HttpFaultServer()
    server.enqueue(Route.rpc("unused"), Stall(phase="headers", gate="unobserved", reply=Reply()))
    server.errors.append("private-error-sentinel")
    monkeypatch.setattr(HttpFaultServer, "active_handlers", property(lambda self: 1))
    result = ScenarioResult("web", "cleanup", "all-failures")

    with pytest.raises(ScenarioFailure) as raised:
        web_scenarios._require_clean(result, server)

    assert raised.value.result is result
    assert result.checks == {
        "server_required_gates_observed": False,
        "server_plan_consumed": False,
        "server_had_no_errors": False,
        "server_handlers_drained": False,
    }
    assert "private-error-sentinel" not in json.dumps(result.events)


@pytest.mark.parametrize("prefix", [b"", b"GET / HTTP/1.1\r\n"])
async def test_connection_abandoned_before_request_does_not_hide_partial_headers(
    prefix: bytes,
) -> None:
    server = HttpFaultServer()

    async def wait_for_handlers(count: int) -> None:
        while server.active_handlers != count:
            await asyncio.sleep(0)

    async with server:
        _reader, writer = await asyncio.open_connection(*server.address)
        await asyncio.wait_for(wait_for_handlers(1), 1)
        if prefix:
            writer.write(prefix)
            await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(wait_for_handlers(0), 1)

    assert server.journal == []
    assert server.committed == []
    if prefix:
        assert server.errors == ["IncompleteReadError"]
    else:
        server.assert_drained()


class _FakeLifecycle:
    def __init__(self) -> None:
        self.open = False

    def is_open(self) -> bool:
        return self.open


class _FakeClient:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self._lifecycle = _FakeLifecycle()
        self._close_error = close_error

    async def __aenter__(self) -> _FakeClient:
        self._lifecycle.open = True
        return self

    async def close(self, *, drain: bool) -> None:
        if self._close_error is not None:
            raise self._close_error
        self._lifecycle.open = False


async def test_logical_transport_refuses_unmapped_host_and_http_downgrade() -> None:
    async with HttpFaultServer() as server:
        transport = LogicalHostTransport({"notebook.google.com": server.address})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError, match="unmapped logical host"):
                await client.get("https://example.com/")
            with pytest.raises(httpx.ConnectError, match="non-HTTPS logical URL"):
                await client.get("http://notebook.google.com/")

    assert server.journal == []


def test_logical_transport_refuses_non_loopback_target() -> None:
    with pytest.raises(ValueError, match="numeric loopback"):
        LogicalHostTransport({"notebook.google.com": ("192.0.2.1", 80)})


async def test_unexpected_request_is_visible_in_server_errors() -> None:
    async with HttpFaultServer() as server:
        transport = LogicalHostTransport({"notebook.google.com": server.address})
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://notebook.google.com/unplanned")

    assert response.status_code == 500
    assert len(server.errors) == 1
    assert "unexpected request" in server.errors[0]


async def test_cohort_records_client_cleanup_failure_and_still_closes_server(monkeypatch) -> None:
    result = ScenarioResult("web", "cleanup", "client-close")
    server = HttpFaultServer()
    fake = _FakeClient(close_error=RuntimeError("client close failed"))
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_args, **_kwargs: fake)

    with pytest.raises(RuntimeError, match="client close failed"):
        async with web_scenarios._cohort(result, server):
            pass

    cleanup = result.events[-1]
    assert cleanup["kind"] == "cleanup"
    assert cleanup["close_error"] == "RuntimeError"
    assert cleanup["server_close_error"] is None
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address


async def test_cohort_records_server_cleanup_failure(monkeypatch) -> None:
    result = ScenarioResult("web", "cleanup", "server-close")
    server = HttpFaultServer()
    fake = _FakeClient()
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_args, **_kwargs: fake)
    original_close = server.aclose

    async def fail_close() -> None:
        raise RuntimeError("server close failed")

    monkeypatch.setattr(server, "aclose", fail_close)
    with pytest.raises(RuntimeError, match="server close failed"):
        async with web_scenarios._cohort(result, server):
            pass

    cleanup = result.events[-1]
    assert cleanup["kind"] == "cleanup"
    assert cleanup["close_error"] is None
    assert cleanup["server_close_error"] == "RuntimeError"

    await original_close()


async def test_cohort_rejects_client_that_silently_remains_open(monkeypatch) -> None:
    class UnclosedClient(_FakeClient):
        async def close(self, *, drain: bool) -> None:
            pass

    result = ScenarioResult("web", "cleanup", "silent-close")
    server = HttpFaultServer()
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_a, **_kw: UnclosedClient())
    with pytest.raises(ScenarioFailure, match="client_closed"):
        async with web_scenarios._cohort(result, server):
            pass
    assert result.checks["client_closed"] is False
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address


@pytest.mark.parametrize(
    "primary_type", [ValueError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
async def test_cohort_preserves_primary_when_cleanup_also_fails(monkeypatch, primary_type) -> None:
    result = ScenarioResult("web", "cleanup", "dual-failure")
    server = HttpFaultServer()
    primary = primary_type("SENTINEL_PRIMARY_CAPABILITY")
    fake = _FakeClient(close_error=RuntimeError("SENTINEL_CLOSE_CAPABILITY"))
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_a, **_kw: fake)
    with pytest.raises(primary_type) as caught:
        async with web_scenarios._cohort(result, server):
            raise primary
    assert caught.value is primary
    cleanup = result.events[-1]
    assert cleanup["primary_error"] == primary_type.__name__
    assert cleanup["close_error"] == "RuntimeError"
    assert "SENTINEL" not in json.dumps(result.events)
    assert server.active_handlers == 0


async def test_web_checks_reject_consumed_action_with_unobserved_required_gate() -> None:
    from tests._fault_server.http import Reply, Route, Transfer

    server = HttpFaultServer()
    route = Route("POST", "notebook.google.com", "/prefix")
    server.enqueue(
        route, Transfer(prefix_bytes=10, gates={"body_prefix": "required"}, response=Reply())
    )
    async with server, server.client_factory() as client:
        await client.post("https://notebook.google.com/prefix", content=b"short")
    assert server.remaining() == 0
    with pytest.raises(ScenarioFailure, match="server_required_gates_observed"):
        web_scenarios._require_clean(ScenarioResult("web", "gate", "missing"), server)


async def test_transfer_chunked_body_commits_only_after_digest_validation() -> None:
    import hashlib

    from tests._fault_server.http import Reply, Route, Transfer

    payload = b"binary\x00asset\xff" * 100
    route = Route("POST", "lh3.googleusercontent.com", "/upload/session-one")
    server = HttpFaultServer(hosts=[route.host])
    server.enqueue(
        route,
        Transfer(
            response=Reply(body=b"confirmed"),
            expected_size=len(payload),
            expected_digest=hashlib.sha256(payload).hexdigest(),
            commit_id="asset-one",
            prefix_bytes=7,
        ),
    )

    async def chunks():
        for offset in range(0, len(payload), 19):
            yield payload[offset : offset + 19]

    async with server:
        async with server.client_factory() as client:
            response = await client.post(f"https://{route.host}{route.path}", content=chunks())
        await server.wait_for_event("handler_settled")
        server.assert_drained()
    assert response.content == b"confirmed"
    record = server.journal[0]
    assert record.body_complete and record.body_bytes == len(payload)
    assert record.body_digest == hashlib.sha256(payload).hexdigest()
    assert server.committed == ["asset-one"]
    phases = [event["phase"] for event in server.events]
    assert phases == [
        "headers",
        "body_prefix",
        "full_body",
        "commit",
        "response_sent",
        "handler_settled",
    ]


async def test_transfer_prefix_disconnect_precedes_complete_request() -> None:
    import asyncio
    import hashlib

    from tests._fault_server.http import Route, Transfer

    route = Route("PUT", "notebook.google.com", "/upload/session-prefix")
    server = HttpFaultServer()
    server.enqueue(route, Transfer(prefix_bytes=4, disconnect_at="body_prefix"))
    async with server:
        reader, writer = await asyncio.open_connection(*server.address)
        try:
            writer.write(
                b"PUT /upload/session-prefix HTTP/1.1\r\nHost: notebook.google.com\r\nContent-Length: 100\r\n\r\n"
            )
            await writer.drain()
            await server.wait_for_event("headers")
            assert server.journal[0].body_bytes == 0
            writer.write(b"abcd")
            await writer.drain()
            await server.wait_for_event("handler_settled")
            assert await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            writer.close()
            await writer.wait_closed()
        server.assert_drained()
    assert server.journal[0].body_bytes == 4
    assert server.journal[0].body_digest == hashlib.sha256(b"abcd").hexdigest()
    assert not server.journal[0].body_complete
    assert server.committed == []
    assert not server.active_handlers


async def test_transfer_commit_loss_retains_independent_commit() -> None:
    import hashlib

    from tests._fault_server.http import Disconnect, Route, Transfer

    route = Route("PUT", "notebook.google.com", "/upload/session-loss")
    server = HttpFaultServer()
    server.enqueue(
        route,
        Transfer(
            response=Disconnect(),
            expected_size=4,
            expected_digest=hashlib.sha256(b"data").hexdigest(),
            commit_id="lost-ack",
        ),
    )
    async with server:
        async with server.client_factory() as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.put("https://notebook.google.com/upload/session-loss", content=b"data")
        await server.wait_for_event("handler_settled")
        server.assert_drained()
    assert server.committed == ["lost-ack"]
    assert server.journal[0].body_complete


async def test_transfer_wrong_body_cannot_commit() -> None:
    import hashlib

    from tests._fault_server.http import Route, Transfer

    route = Route("PUT", "notebook.google.com", "/upload/session-digest")
    server = HttpFaultServer()
    server.enqueue(
        route,
        Transfer(
            expected_size=4,
            expected_digest=hashlib.sha256(b"good").hexdigest(),
            commit_id="forbidden",
        ),
    )
    async with server:
        async with server.client_factory() as client:
            response = await client.put(
                "https://notebook.google.com/upload/session-digest", content=b"evil"
            )
        await server.wait_for_event("handler_settled")
    assert response.status_code == 500
    assert server.committed == []
    with pytest.raises(AssertionError, match="server errors"):
        server.assert_drained()


@pytest.mark.parametrize(
    "framing,body",
    [
        (b"Content-Length: -1", b""),
        (b"Content-Length: 100", b""),
        (b"Content-Length: 0\r\nTransfer-Encoding: chunked", b"0\r\n\r\n"),
        (b"Transfer-Encoding: chunked", b"xx\r\n"),
        (b"Transfer-Encoding: chunked", b"20\r\n"),
        (b"Content-Length: 0\r\nContent-Length: 0", b""),
    ],
)
async def test_malformed_or_oversized_transfer_fails_and_settles(framing, body) -> None:
    import asyncio

    from tests._fault_server.http import Route, Transfer

    server = HttpFaultServer(max_body=16)
    server.enqueue(Route("PUT", "notebook.google.com", "/upload"), Transfer())
    async with server:
        reader, writer = await asyncio.open_connection(*server.address)
        try:
            writer.write(
                b"PUT /upload HTTP/1.1\r\nHost: notebook.google.com\r\n"
                + framing
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 1)
            assert response.startswith(b"HTTP/1.1 500")
        finally:
            writer.close()
            await writer.wait_closed()
    assert not server.active_handlers
    assert server.committed == []
    assert server.errors


async def test_abandoned_gated_body_is_settled_by_server_close() -> None:
    import asyncio

    from tests._fault_server.http import Route, Transfer

    server = HttpFaultServer()
    server.enqueue(
        Route("PUT", "notebook.google.com", "/upload"), Transfer(gates={"headers": "hold"})
    )
    async with server:
        reader, writer = await asyncio.open_connection(*server.address)
        writer.write(
            b"PUT /upload HTTP/1.1\r\nHost: notebook.google.com\r\nContent-Length: 10\r\n\r\n"
        )
        await writer.drain()
        await server.wait_for_event("headers")
        assert server.active_handlers == 1
    try:
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert server.active_handlers == 0
        assert server.events[-1]["phase"] == "handler_settled"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_keep_alive_proves_reuse_then_peer_close_recovery() -> None:
    from tests._fault_server.http import Reply, Route

    server = HttpFaultServer(keep_alive=True)
    route = Route.homepage()
    server.enqueue(
        route,
        Reply(body=b"one"),
        Reply(body=b"two", headers={"Connection": "close"}),
        Reply(body=b"three"),
    )
    async with server:
        async with server.client_factory() as client:
            for expected in (b"one", b"two", b"three"):
                assert (await client.get("https://notebook.google.com/")).content == expected
        server.assert_drained()
    connections = [record.connection_id for record in server.journal]
    assert connections[0] == connections[1]
    assert connections[2] != connections[1]


@pytest.mark.parametrize("allowed", [False, True])
async def test_keep_alive_transfer_does_not_allow_next_request_partial_headers(
    allowed: bool,
) -> None:
    from tests._fault_server.http import Transfer

    server = HttpFaultServer(keep_alive=True)
    server.enqueue(
        Route("PUT", "notebook.google.com", "/upload"),
        Transfer(allow_abandoned_body=allowed, response=Reply(body=b"ok")),
    )
    async with server:
        reader, writer = await asyncio.open_connection(*server.address)
        writer.write(
            b"PUT /upload HTTP/1.1\r\nHost: notebook.google.com\r\nContent-Length: 0\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
        assert await asyncio.wait_for(reader.readexactly(2), 1) == b"ok"
        writer.write(b"GET / HTTP/1.1\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await server.wait_for_event("handler_settled")

    assert server.errors == ["IncompleteReadError"]
    assert len(server.journal) == 1
    assert server.journal[0].body_complete
    assert not any(event["phase"] == "body_abandoned" for event in server.events)


async def test_unused_required_prefix_gate_cannot_pass() -> None:
    from tests._fault_server.http import Route, Transfer

    server = HttpFaultServer()
    server.enqueue(
        Route("PUT", "notebook.google.com", "/upload"),
        Transfer(prefix_bytes=100, gates={"body_prefix": "required-prefix"}),
    )
    async with server, server.client_factory() as client:
        assert (
            await client.put("https://notebook.google.com/upload", content=b"short")
        ).status_code == 200
    with pytest.raises(AssertionError, match="unobserved required"):
        server.assert_drained()


@pytest.mark.parametrize("allowed", [False, True])
async def test_abandoned_body_requires_explicit_fault_contract(allowed: bool) -> None:
    import asyncio

    from tests._fault_server.http import Route, Transfer

    server = HttpFaultServer()
    server.enqueue(
        Route("PUT", "notebook.google.com", "/upload"),
        Transfer(
            prefix_bytes=4,
            gates={"body_prefix": "prefix"},
            allow_abandoned_body=allowed,
        ),
    )
    async with server:
        _reader, writer = await asyncio.open_connection(*server.address)
        writer.write(
            b"PUT /upload HTTP/1.1\r\nHost: notebook.google.com\r\nContent-Length: 10\r\n\r\npart"
        )
        await writer.drain()
        await server.wait_for_gate("prefix")
        writer.close()
        await writer.wait_closed()
        server.release("prefix")
        await server.wait_for_event("handler_settled")
    assert not server.journal[0].body_complete
    assert server.journal[0].body_bytes == 4
    assert server.committed == []
    if allowed:
        server.assert_drained()
        assert any(event["phase"] == "body_abandoned" for event in server.events)
    else:
        with pytest.raises(AssertionError, match="IncompleteReadError"):
            server.assert_drained()


async def test_upload_route_rejects_wrong_session_capability() -> None:
    from tests._fault_server.http import Route, Transfer

    server = HttpFaultServer()
    server.enqueue(Route("PUT", "notebook.google.com", "/upload", upload_id="expected"), Transfer())
    async with server, server.client_factory() as client:
        response = await client.put(
            "https://notebook.google.com/upload?upload_id=wrong-secret", content=b"data"
        )
    assert response.status_code == 500
    assert server.journal == []
    assert server.remaining() == 1
    assert "wrong-secret" not in str(server.errors)
    with pytest.raises(AssertionError, match="unexpected request"):
        server.assert_drained()


def _upload_session_reply(upload_id: str) -> Reply:
    return Reply(
        headers={
            "X-Goog-Upload-URL": (
                "https://notebook.google.com/upload/session"
                f"?upload_protocol=resumable&upload_id={upload_id}"
            )
        }
    )


async def test_required_upload_session_rejects_finalize_without_issued_start() -> None:
    from tests._fault_server.http import Route, Transfer

    finalize = Route("PUT", "notebook.google.com", "/upload/session", upload_id="never-issued")
    server = HttpFaultServer()
    server.enqueue(finalize, Transfer(require_session=True, commit_id=None))

    async with server, server.client_factory() as client:
        response = await client.put(
            "https://notebook.google.com/upload/session?upload_id=never-issued",
            content=b"must-not-be-consumed",
        )
        await server.wait_for_event("handler_settled")

    assert response.status_code == 500
    assert server.committed == []
    assert server.journal[0].body_bytes == 0
    assert not server.journal[0].body_complete


async def test_required_upload_session_rejects_missing_id_before_body() -> None:
    from tests._fault_server.http import Route, Transfer

    finalize = Route("PUT", "notebook.google.com", "/upload/session")
    server = HttpFaultServer()
    server.enqueue(finalize, Transfer(require_session=True))

    async with server, server.client_factory() as client:
        response = await client.put(
            "https://notebook.google.com/upload/session",
            content=b"must-not-be-consumed",
        )
        await server.wait_for_event("handler_settled")

    assert response.status_code == 500
    assert server.committed == []
    assert server.journal[0].body_bytes == 0
    assert not server.journal[0].body_complete


async def test_required_upload_session_rejects_wrong_issued_id() -> None:
    from tests._fault_server.http import Route, Transfer

    start = Route("POST", "notebook.google.com", "/upload/start")
    wrong_finalize = Route(
        "PUT", "notebook.google.com", "/upload/session", upload_id="wrong-session"
    )
    server = HttpFaultServer()
    server.enqueue(start, _upload_session_reply("issued-session"))
    server.enqueue(wrong_finalize, Transfer(require_session=True))

    async with server, server.client_factory() as client:
        assert (
            await client.post("https://notebook.google.com/upload/start", content=b"")
        ).status_code == 200
        response = await client.put(
            "https://notebook.google.com/upload/session?upload_id=wrong-session",
            content=b"must-not-be-consumed",
        )
        await server.wait_for_event("handler_settled", count=2)

    assert response.status_code == 500
    assert server.committed == []
    assert server.journal[-1].body_bytes == 0
    assert not server.journal[-1].body_complete


async def test_required_upload_session_rejects_duplicate_finalize() -> None:
    import hashlib

    from tests._fault_server.http import Route, Transfer

    payload = b"one committed body"
    start = Route("POST", "notebook.google.com", "/upload/start")
    finalize = Route("PUT", "notebook.google.com", "/upload/session", upload_id="issued-session")
    transfer = Transfer(
        require_session=True,
        expected_size=len(payload),
        expected_digest=hashlib.sha256(payload).hexdigest(),
        commit_id="first",
    )
    server = HttpFaultServer()
    server.enqueue(start, _upload_session_reply("issued-session"))
    server.enqueue(finalize, transfer, Transfer(require_session=True))

    async with server, server.client_factory() as client:
        await client.post("https://notebook.google.com/upload/start", content=b"")
        assert (
            await client.put(
                "https://notebook.google.com/upload/session?upload_id=issued-session",
                content=payload,
            )
        ).status_code == 200
        duplicate = await client.put(
            "https://notebook.google.com/upload/session?upload_id=issued-session",
            content=b"must-not-be-consumed",
        )
        await server.wait_for_event("handler_settled", count=3)

    assert duplicate.status_code == 500
    assert server.committed == ["first"]
    assert server.journal[-1].body_bytes == 0
    assert not server.journal[-1].body_complete


async def test_required_upload_session_remains_active_without_validated_commit() -> None:
    import hashlib

    from tests._fault_server.http import Route, Transfer

    payload = b"retry after rejected finalize"
    start = Route("POST", "notebook.google.com", "/upload/start")
    finalize = Route("PUT", "notebook.google.com", "/upload/session", upload_id="retry-session")
    server = HttpFaultServer()
    server.enqueue(start, _upload_session_reply("retry-session"))
    server.enqueue(
        finalize,
        Transfer(require_session=True, response=Reply(403)),
        Transfer(
            require_session=True,
            expected_size=len(payload),
            expected_digest=hashlib.sha256(payload).hexdigest(),
            commit_id="retry-commit",
        ),
    )

    async with server, server.client_factory() as client:
        await client.post("https://notebook.google.com/upload/start", content=b"")
        assert (
            await client.put(
                "https://notebook.google.com/upload/session?upload_id=retry-session",
                content=payload,
            )
        ).status_code == 403
        assert (
            await client.put(
                "https://notebook.google.com/upload/session?upload_id=retry-session",
                content=payload,
            )
        ).status_code == 200
        await server.wait_for_event("handler_settled", count=3)

    assert server.committed == ["retry-commit"]
    server.assert_drained()


async def test_required_upload_session_rejects_cancelled_session() -> None:
    from tests._fault_server.http import Reply, Route, Transfer

    start = Route("POST", "notebook.google.com", "/upload/start")
    session = Route("PUT", "notebook.google.com", "/upload/session", upload_id="cancelled-session")
    server = HttpFaultServer()
    server.enqueue(start, _upload_session_reply("cancelled-session"))
    server.enqueue(session, Reply(), Transfer(require_session=True))

    async with server, server.client_factory() as client:
        await client.post("https://notebook.google.com/upload/start", content=b"")
        assert (
            await client.put(
                "https://notebook.google.com/upload/session?upload_id=cancelled-session",
                headers={"X-Goog-Upload-Command": "cancel"},
            )
        ).status_code == 200
        finalize = await client.put(
            "https://notebook.google.com/upload/session?upload_id=cancelled-session",
            content=b"must-not-be-consumed",
        )
        await server.wait_for_event("handler_settled", count=3)

    assert finalize.status_code == 500
    assert server.committed == []
    assert server.journal[-1].body_bytes == 0
    assert not server.journal[-1].body_complete


def test_assert_drained_reports_only_generic_pending_action_count() -> None:
    from tests._fault_server.http import Route, Transfer

    server = HttpFaultServer()
    server.enqueue(
        Route(
            "PUT",
            "private-upload.example.com",
            "/capability/private-path",
            upload_id="private-upload-id",
        ),
        Transfer(),
    )

    with pytest.raises(AssertionError, match=r"^unconsumed HTTP actions: 1$") as exc_info:
        server.assert_drained()
    assert "private" not in str(exc_info.value)
