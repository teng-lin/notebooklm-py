"""Bounded real-socket HTTP/1.1 fault server and logical-host router."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx

_MAX_HEADERS = 64 * 1024
_MAX_BODY = 2 * 1024 * 1024
_READ_TIMEOUT = 2.0
_CLOSE_TIMEOUT = 2.0


@dataclass(frozen=True)
class Route:
    """Exact logical request route; query parameters are inspected separately."""

    method: str
    host: str
    path: str
    rpc_id: str | None = None

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


Action = Reply | Disconnect | Truncate | Stall


@dataclass(frozen=True)
class RequestRecord:
    sequence: int
    route: Route
    query: Mapping[str, tuple[str, ...]]
    form: Mapping[str, tuple[str, ...]]
    cookie_names: tuple[str, ...]
    cookie_values: Mapping[str, str]
    action: str

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

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.port not in (None, 443):
            raise httpx.ConnectError(
                f"fault transport refuses non-HTTPS logical URL {request.url!s}",
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

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._scripts: dict[Route, deque[Action]] = defaultdict(deque)
        self._gates: dict[str, asyncio.Event] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._changed = asyncio.Condition()
        self.journal: list[RequestRecord] = []
        self.committed: list[str] = []
        self.errors: list[str] = []

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

    def gate(self, name: str) -> asyncio.Event:
        if not name:
            raise ValueError("gate name must be nonempty")
        return self._gates.setdefault(name, asyncio.Event())

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
        kwargs["transport"] = LogicalHostTransport(
            {
                "notebook.google.com": self.address,
                "accounts.google.com": self.address,
            }
        )
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
            await asyncio.wait_for(server.wait_closed(), _CLOSE_TIMEOUT)
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

    @property
    def active_handlers(self) -> int:
        return sum(not task.done() for task in self._handlers)

    def remaining(self) -> int:
        return sum(len(actions) for actions in self._scripts.values())

    def assert_drained(self) -> None:
        if self.errors:
            raise AssertionError("HTTP fault server errors: " + "; ".join(self.errors))
        remaining = {route: len(actions) for route, actions in self._scripts.items() if actions}
        if remaining:
            raise AssertionError(f"unconsumed HTTP actions: {remaining!r}")

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        self._writers.add(writer)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        expected_disconnect = False
        try:
            route, query, form, cookie_values = await self._read_request(reader)
            actions = self._scripts.get(route)
            if not actions:
                raise AssertionError(f"unexpected request: {route!r}")
            action = actions.popleft()
            record = RequestRecord(
                sequence=len(self.journal) + 1,
                route=route,
                query=query,
                form=form,
                cookie_names=tuple(sorted(cookie_values)),
                cookie_values=cookie_values,
                action=type(action).__name__,
            )
            self.journal.append(record)
            async with self._changed:
                self._changed.notify_all()
            expected_disconnect = isinstance(action, (Disconnect, Truncate, Stall))
            await self._run_action(action, writer)
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError) as exc:
            if not expected_disconnect:
                self.errors.append(f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await self._write_reply(writer, Reply(500, str(exc).encode()))
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[
        Route,
        Mapping[str, tuple[str, ...]],
        Mapping[str, tuple[str, ...]],
        Mapping[str, str],
    ]:
        raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _READ_TIMEOUT)
        if len(raw_headers) > _MAX_HEADERS:
            raise ValueError("request headers exceed limit")
        lines = raw_headers[:-4].split(b"\r\n")
        method_raw, target_raw, version = lines[0].split(b" ", 2)
        if version != b"HTTP/1.1":
            raise ValueError("only HTTP/1.1 is supported")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if not separator:
                raise ValueError("malformed request header")
            headers[name.decode("ascii").lower()] = value.decode("latin-1").strip()
        if "transfer-encoding" in headers:
            raise ValueError("chunked requests are unsupported")
        length = int(headers.get("content-length", "0"))
        if length < 0 or length > _MAX_BODY:
            raise ValueError("request body exceeds limit")
        body = await asyncio.wait_for(reader.readexactly(length), _READ_TIMEOUT) if length else b""
        target = urlsplit(target_raw.decode("ascii"))
        query_raw = parse_qs(target.query, keep_blank_values=True)
        form_raw = parse_qs(body.decode("utf-8"), keep_blank_values=True) if body else {}
        query = {key: tuple(values) for key, values in query_raw.items()}
        form = {key: tuple(values) for key, values in form_raw.items()}
        cookie_values: dict[str, str] = {}
        for part in headers.get("cookie", "").split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                cookie_values[name] = value
        rpc_values = query.get("rpcids", ())
        route = Route(
            method_raw.decode("ascii"),
            headers.get("host", ""),
            target.path,
            rpc_values[0] if rpc_values else None,
        )
        return route, query, form, cookie_values

    async def _run_action(self, action: Action, writer: asyncio.StreamWriter) -> None:
        if isinstance(action, Reply):
            await self._write_reply(writer, action)
        elif isinstance(action, Disconnect):
            if action.commit_id is not None:
                self.committed.append(action.commit_id)
        elif isinstance(action, Truncate):
            headers = {"Content-Length": str(action.declared_length), "Connection": "close"}
            await self._write_head(writer, action.status, headers)
            writer.write(action.body)
            await writer.drain()
        else:
            if action.phase == "headers":
                await self.gate(action.gate).wait()
                await self._write_reply(writer, action.reply)
            else:
                declared = len(action.reply.body)
                headers = dict(action.reply.headers)
                headers["Content-Length"] = str(declared)
                headers["Connection"] = "close"
                await self._write_head(writer, action.reply.status, headers)
                writer.write(action.prefix)
                await writer.drain()
                await self.gate(action.gate).wait()
                writer.write(action.reply.body[len(action.prefix) :])
                await writer.drain()

    async def _write_reply(self, writer: asyncio.StreamWriter, reply: Reply) -> None:
        headers = dict(reply.headers)
        headers.setdefault("Content-Length", str(len(reply.body)))
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        headers.setdefault("Connection", "close")
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
]
