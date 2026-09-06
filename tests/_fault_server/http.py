"""Bounded real-socket HTTP/1.1 fault server and logical-host router."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections import Counter, defaultdict, deque
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx

from .http_framing import BodyDigest, iter_body, read_head

_MAX_BODY = 2 * 1024 * 1024
_CLOSE_TIMEOUT = 2.0


@dataclass(frozen=True)
class Route:
    """Exact logical request route; query parameters are inspected separately."""

    method: str
    host: str
    path: str
    rpc_id: str | None = None
    upload_id: str | None = None

    @classmethod
    def rpc(cls, rpc_id: str) -> Route:
        return cls(
            "POST",
            "notebook.google.com",
            "/_/LabsTailwindUi/data/batchexecute",
            rpc_id,
        )

    @classmethod
    def homepage(cls) -> Route:
        return cls("GET", "notebook.google.com", "/")

    @classmethod
    def login(cls) -> Route:
        return cls("GET", "accounts.google.com", "/ServiceLogin")


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Disconnect:
    """Close after reading the full request, before response headers."""

    commit_id: str | None = None


@dataclass(frozen=True)
class Truncate:
    """Advertise a longer body than is written, then close the socket."""

    body: bytes
    declared_length: int
    status: int = 200


@dataclass(frozen=True)
class Stall:
    """Wait on a named gate before headers or after a response prefix."""

    phase: Literal["headers", "body"]
    gate: str
    reply: Reply
    prefix: bytes = b""


@dataclass(frozen=True)
class Transfer:
    """Select a fault at headers, before consuming a transfer body.

    Commit evidence requires an independently supplied expected size and digest.
    Gate names correspond to headers, body_prefix, full_body, and commit events.
    """

    response: Reply | Disconnect | Truncate | Stall = field(default_factory=Reply)
    prefix_bytes: int = 65536
    gates: Mapping[str, str] = field(default_factory=dict)
    disconnect_at: Literal["headers", "body_prefix", "full_body"] | None = None
    expected_size: int | None = None
    expected_digest: str | None = None
    commit_id: str | None = None
    allow_abandoned_body: bool = False
    require_session: bool = False

    def __post_init__(self) -> None:
        if self.prefix_bytes < 1:
            raise ValueError("transfer prefix must be positive")
        if set(self.gates) - {"headers", "body_prefix", "full_body", "commit"}:
            raise ValueError("unknown transfer gate phase")
        if self.commit_id is not None and (
            self.expected_size is None or self.expected_digest is None
        ):
            raise ValueError("transfer commit requires independent body expectations")


Action = Reply | Disconnect | Truncate | Stall | Transfer


@dataclass
class RequestRecord:
    sequence: int
    route: Route
    query: Mapping[str, tuple[str, ...]]
    form: Mapping[str, tuple[str, ...]]
    cookie_names: tuple[str, ...]
    cookie_values: Mapping[str, str]
    action: str
    connection_id: int = 0
    body_bytes: int = 0
    body_digest: str = ""
    body_complete: bool = False
    response_status: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def csrf(self) -> str | None:
        values = self.form.get("at", ())
        return values[0] if values else None

    @property
    def session_id(self) -> str | None:
        values = self.query.get("f.sid", ())
        return values[0] if values else None


class LogicalHostTransport(httpx.AsyncBaseTransport):
    """Keep logical URL semantics while connecting only to a local server."""

    def __init__(self, routes: Mapping[str, tuple[str, int]]) -> None:
        self._routes = dict(routes)
        for logical_host, (physical_host, physical_port) in self._routes.items():
            if not logical_host or not ipaddress.ip_address(physical_host).is_loopback:
                raise ValueError("fault transport routes must target numeric loopback addresses")
            if not 1 <= physical_port <= 65535:
                raise ValueError("fault transport route port is invalid")
        self._inner = httpx.AsyncHTTPTransport(proxy=None, trust_env=False)

    def retarget(self, logical_host: str, target: tuple[str, int]) -> None:
        """Redirect one logical host between attempts on this transport instance."""
        physical_host, physical_port = target
        if logical_host not in self._routes:
            raise ValueError(f"unknown logical host: {logical_host}")
        if not ipaddress.ip_address(physical_host).is_loopback:
            raise ValueError("fault transport target must be numeric loopback")
        if not 1 <= physical_port <= 65535:
            raise ValueError("fault transport target port is invalid")
        self._routes[logical_host] = (physical_host, physical_port)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.port not in (None, 443):
            raise httpx.ConnectError(
                "fault transport refuses non-HTTPS logical URL",
                request=request,
            )
        target = self._routes.get(request.url.host)
        if target is None:
            raise httpx.ConnectError(
                f"fault transport refuses unmapped logical host {request.url.host!r}",
                request=request,
            )
        physical_host, physical_port = target
        physical_url = request.url.copy_with(
            scheme="http",
            host=physical_host,
            port=physical_port,
        )
        headers = list(request.headers.raw)
        logical_host = request.url.host.encode("ascii")
        headers = [
            (name, logical_host if name.lower() == b"host" else value) for name, value in headers
        ]
        physical_request = httpx.Request(
            request.method,
            physical_url,
            headers=headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        response = await self._inner.handle_async_request(physical_request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=response.stream,
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class HttpFaultServer:
    """One-use scripted server with explicit journals, gates, and cleanup."""

    def __init__(
        self, *, hosts: Iterable[str] = (), keep_alive: bool = False, max_body: int = _MAX_BODY
    ) -> None:
        if not 1 <= max_body <= 16 * 1024 * 1024:
            raise ValueError("fault body limit must be between 1 and 16 MiB")
        self._hosts = {"notebook.google.com", "accounts.google.com", *hosts}
        self.keep_alive = keep_alive
        self.max_body = max_body
        self.events: list[dict[str, Any]] = []
        self._connection_sequence = 0
        self._server: asyncio.Server | None = None
        self._scripts: dict[Route, deque[Action]] = defaultdict(deque)
        self._gates: dict[str, asyncio.Event] = {}
        self._required_gates: Counter[str] = Counter()
        self._observed_gates: Counter[str] = Counter()
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._changed = asyncio.Condition()
        self.journal: list[RequestRecord] = []
        self.committed: list[str] = []
        self.errors: list[str] = []
        self._upload_sessions: dict[
            tuple[str, str, str], Literal["issued", "committed", "cancelled"]
        ] = {}

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("HTTP fault server is not running")
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    def enqueue(self, route: Route, *actions: Action) -> None:
        if self._server is not None:
            raise RuntimeError("enqueue scripts before starting the server")
        if not actions:
            raise ValueError("at least one action is required")
        self._scripts[route].extend(actions)
        for action in actions:
            if isinstance(action, Transfer):
                self._required_gates.update(action.gates.values())
                if isinstance(action.response, Stall):
                    self._required_gates[action.response.gate] += 1
            elif isinstance(action, Stall):
                self._required_gates[action.gate] += 1

    def gate(self, name: str) -> asyncio.Event:
        if not name:
            raise ValueError("gate name must be nonempty")
        return self._gates.setdefault(name, asyncio.Event())

    async def _wait_gate(self, name: str) -> None:
        self._observed_gates[name] += 1
        async with self._changed:
            self._changed.notify_all()
        await self.gate(name).wait()

    def release(self, name: str) -> None:
        self.gate(name).set()

    async def wait_for_requests(self, route: Route, count: int, *, timeout: float = 2.0) -> None:
        async def _wait() -> None:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: sum(record.route == route for record in self.journal) >= count
                )

        await asyncio.wait_for(_wait(), timeout)

    def client_factory(self, **kwargs: Any) -> httpx.AsyncClient:
        """Factory compatible with the production kernel construction seam."""
        if "transport" in kwargs:
            raise TypeError("fault client factory owns the transport")
        kwargs["transport"] = LogicalHostTransport(dict.fromkeys(self._hosts, self.address))
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)

    async def __aenter__(self) -> HttpFaultServer:
        if self._server is not None:
            raise RuntimeError("HTTP fault server already started")
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        # Python 3.12+ wait_closed also waits for accepted connections. Close
        # their writers and cancel held handlers before awaiting the listener.
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self._handlers), return_exceptions=True),
                _CLOSE_TIMEOUT,
            )
        if self._writers:
            await asyncio.wait_for(
                asyncio.gather(
                    *(writer.wait_closed() for writer in tuple(self._writers)),
                    return_exceptions=True,
                ),
                _CLOSE_TIMEOUT,
            )

        if server is not None:
            await asyncio.wait_for(server.wait_closed(), _CLOSE_TIMEOUT)

    @property
    def active_handlers(self) -> int:
        return sum(not task.done() for task in self._handlers)

    def remaining(self) -> int:
        return sum(len(actions) for actions in self._scripts.values())

    def assert_drained(self) -> None:
        if self.errors:
            raise AssertionError("HTTP fault server errors: " + "; ".join(self.errors))
        if self._required_gates - self._observed_gates:
            raise AssertionError("unobserved required HTTP gates")
        remaining = sum(len(actions) for actions in self._scripts.values())
        if remaining:
            raise AssertionError(f"unconsumed HTTP actions: {remaining}")

    async def wait_for_gate(self, name: str, *, count: int = 1, timeout: float = 2.0) -> None:
        async def wait() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: self._observed_gates[name] >= count)

        await asyncio.wait_for(wait(), timeout)

    async def wait_for_event(self, phase: str, *, count: int = 1, timeout: float = 2.0) -> None:
        async def wait() -> None:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: sum(event["phase"] == phase for event in self.events) >= count
                )

        await asyncio.wait_for(wait(), timeout)

    async def _event(self, phase: str, record: RequestRecord) -> None:
        self.events.append(
            {
                "phase": phase,
                "request": record.sequence,
                "connection_id": record.connection_id,
                "body_bytes": record.body_bytes,
                "body_digest": record.body_digest,
            }
        )
        async with self._changed:
            self._changed.notify_all()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connection_sequence += 1
        task = asyncio.create_task(self._handle(reader, writer, self._connection_sequence))
        self._handlers.add(task)
        self._writers.add(writer)
        task.add_done_callback(self._handlers.discard)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, connection_id: int
    ) -> None:
        record: RequestRecord | None = None
        expected_disconnect = False
        transfer: Transfer | None = None
        try:
            while True:
                try:
                    head = await read_head(reader)
                except asyncio.IncompleteReadError as exc:
                    if not exc.partial and record is not None and self.keep_alive:
                        break
                    raise
                target = urlsplit(head.target)
                query = {
                    k: tuple(v) for k, v in parse_qs(target.query, keep_blank_values=True).items()
                }
                rpc_values = query.get("rpcids", ())
                route = Route(
                    head.method,
                    head.headers["host"],
                    target.path,
                    rpc_values[0] if rpc_values else None,
                    query.get("upload_id", (None,))[0],
                )
                actions = self._scripts.get(route)
                if not actions:
                    raise AssertionError("unexpected request")
                action = actions.popleft()
                transfer = action if isinstance(action, Transfer) else None
                cookies = {}
                for part in head.headers.get("cookie", "").split(";"):
                    name, separator, value = part.strip().partition("=")
                    if separator and name:
                        cookies[name] = value
                record = RequestRecord(
                    sequence=len(self.journal) + 1,
                    route=route,
                    query=query,
                    form={},
                    cookie_names=tuple(sorted(cookies)),
                    cookie_values=cookies,
                    action=type(action).__name__,
                    connection_id=connection_id,
                    headers=head.headers,
                )
                self.journal.append(record)
                expected_disconnect = isinstance(action, (Disconnect, Truncate, Stall, Transfer))

                session_key: tuple[str, str, str] | None = None
                if transfer is not None and transfer.require_session:
                    session_key = self._require_active_upload_session(route)

                async def phase(
                    name: str, record: RequestRecord = record, transfer: Transfer | None = transfer
                ) -> bool:
                    assert record is not None
                    await self._event(name, record)
                    if transfer is not None:
                        if gate := transfer.gates.get(name):
                            await self._wait_gate(gate)
                        if transfer.disconnect_at == name:
                            await self._event("disconnect", record)
                            return True
                    return False

                if await phase("headers"):
                    break
                digest = BodyDigest()
                form_body = bytearray()
                prefix_observed = False
                interrupted = False
                async for chunk in iter_body(
                    reader,
                    head.headers,
                    limit=self.max_body,
                    block_size=min(65536, transfer.prefix_bytes) if transfer else 65536,
                ):
                    digest.update(chunk)
                    record.body_bytes = digest.size
                    record.body_digest = digest.hexdigest
                    if route.rpc_id is not None:
                        form_body.extend(chunk)
                    if transfer and not prefix_observed and digest.size >= transfer.prefix_bytes:
                        prefix_observed = True
                        if await phase("body_prefix"):
                            interrupted = True
                            break
                if interrupted:
                    break
                record.body_digest = digest.hexdigest
                record.body_complete = True
                if form_body:
                    record.form = {
                        k: tuple(v)
                        for k, v in parse_qs(
                            form_body.decode("utf-8"), keep_blank_values=True
                        ).items()
                    }
                if await phase("full_body"):
                    break
                if transfer:
                    if transfer.expected_size is not None and digest.size != transfer.expected_size:
                        raise AssertionError("transfer byte count mismatch")
                    if (
                        transfer.expected_digest is not None
                        and digest.hexdigest != transfer.expected_digest
                    ):
                        raise AssertionError("transfer digest mismatch")
                    if transfer.commit_id is not None:
                        if transfer.commit_id in self.committed:
                            raise AssertionError("duplicate transfer commit")
                        self.committed.append(transfer.commit_id)
                        if session_key is not None:
                            self._upload_sessions[session_key] = "committed"
                        await phase("commit")
                    response = transfer.response
                else:
                    response = action
                await self._run_action(response, writer, record)
                if isinstance(response, Reply) and 200 <= response.status < 300:
                    self._record_upload_session(response)
                    self._record_upload_cancellation(record)
                await self._event("response_sent", record)
                if not self.keep_alive or not isinstance(response, Reply):
                    break
                if head.headers.get("connection", "").lower() == "close":
                    break
                if any(
                    k.lower() == "connection" and v.lower() == "close"
                    for k, v in response.headers.items()
                ):
                    break
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError:
            if transfer is None or not transfer.allow_abandoned_body:
                self.errors.append("IncompleteReadError")
            elif record is not None:
                await self._event("body_abandoned", record)
        except (BrokenPipeError, ConnectionResetError) as exc:
            if not expected_disconnect:
                self.errors.append(type(exc).__name__)
        except Exception as exc:
            # Exception text can contain a capability URL or malformed body bytes.
            label = (
                "unexpected request"
                if isinstance(exc, AssertionError) and str(exc) == "unexpected request"
                else type(exc).__name__
            )
            self.errors.append(label)
            with contextlib.suppress(Exception):
                await self._write_reply(writer, Reply(500, b"fault service request failed"))
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), _CLOSE_TIMEOUT)
            self._writers.discard(writer)
            if record is not None:
                await self._event("handler_settled", record)

    def _require_active_upload_session(self, route: Route) -> tuple[str, str, str]:
        if route.upload_id is None:
            raise AssertionError("upload session required")
        key = (route.host, route.path, route.upload_id)
        if self._upload_sessions.get(key) != "issued":
            raise AssertionError("upload session is not active")
        return key

    def _record_upload_session(self, reply: Reply) -> None:
        upload_url = next(
            (value for name, value in reply.headers.items() if name.lower() == "x-goog-upload-url"),
            None,
        )
        if upload_url is None:
            return
        target = urlsplit(upload_url)
        upload_ids = parse_qs(target.query, keep_blank_values=True).get("upload_id", [])
        if target.scheme != "https" or target.hostname is None or len(upload_ids) != 1:
            raise AssertionError("invalid upload session URL")
        upload_id = upload_ids[0]
        if not target.path or not upload_id:
            raise AssertionError("invalid upload session URL")
        self._upload_sessions[(target.hostname, target.path, upload_id)] = "issued"

    def _record_upload_cancellation(self, record: RequestRecord) -> None:
        commands = {
            command.strip().lower()
            for command in record.headers.get("x-goog-upload-command", "").split(",")
        }
        if "cancel" not in commands or record.route.upload_id is None:
            return
        key = (record.route.host, record.route.path, record.route.upload_id)
        if key in self._upload_sessions:
            self._upload_sessions[key] = "cancelled"

    async def _run_action(
        self,
        action: Reply | Disconnect | Truncate | Stall,
        writer: asyncio.StreamWriter,
        record: RequestRecord,
    ) -> None:
        record.response_status = (
            action.status
            if isinstance(action, (Reply, Truncate))
            else action.reply.status
            if isinstance(action, Stall)
            else None
        )
        if isinstance(action, Reply):
            await self._write_reply(writer, action)
        elif isinstance(action, Disconnect):
            if action.commit_id is not None:
                self.committed.append(action.commit_id)
                await self._event("commit", record)
            await self._event("disconnect", record)
        elif isinstance(action, Truncate):
            headers = {"Content-Length": str(action.declared_length), "Connection": "close"}
            await self._write_head(writer, action.status, headers)
            writer.write(action.body)
            await writer.drain()
            await self._event("response_prefix", record)
        else:
            if action.phase == "headers":
                await self._wait_gate(action.gate)
                await self._write_reply(writer, action.reply)
            else:
                declared = len(action.reply.body)
                headers = dict(action.reply.headers)
                headers["Content-Length"] = str(declared)
                headers["Connection"] = "close"
                await self._write_head(writer, action.reply.status, headers)
                writer.write(action.prefix)
                await writer.drain()
                await self._event("response_prefix", record)
                await self._wait_gate(action.gate)
                writer.write(action.reply.body[len(action.prefix) :])
                await writer.drain()

    async def _write_reply(self, writer: asyncio.StreamWriter, reply: Reply) -> None:
        headers = dict(reply.headers)
        headers.setdefault("Content-Length", str(len(reply.body)))
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        headers.setdefault("Connection", "keep-alive" if self.keep_alive else "close")
        await self._write_head(writer, reply.status, headers)
        writer.write(reply.body)
        await writer.drain()

    @staticmethod
    async def _write_head(
        writer: asyncio.StreamWriter, status: int, headers: Mapping[str, str]
    ) -> None:
        reason = {
            200: "OK",
            302: "Found",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            429: "Too Many Requests",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status, "Response")
        lines: Iterable[str] = (
            f"HTTP/1.1 {status} {reason}",
            *(f"{name}: {value}" for name, value in headers.items()),
            "",
            "",
        )
        writer.write("\r\n".join(lines).encode("latin-1"))
        await writer.drain()


async def iter_records(server: HttpFaultServer, route: Route) -> AsyncIterator[RequestRecord]:
    """Yield the current route-specific journal without exposing its list shape."""
    for record in server.journal:
        if record.route == route:
            yield record


__all__ = [
    "Disconnect",
    "HttpFaultServer",
    "LogicalHostTransport",
    "Reply",
    "RequestRecord",
    "Route",
    "Stall",
    "Truncate",
    "Transfer",
]
