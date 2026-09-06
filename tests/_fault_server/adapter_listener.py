"""Live ASGI listener and caller-abort gate; no framework transport substitutes."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager, nullcontext
from typing import Any

import uvicorn


class DisconnectGate:
    """Pause the selected response after a prefix until a real peer disconnect.

    A receive pump preserves every ASGI request message for the app and observes
    the listener's actual http.disconnect. It never manufactures cancellation or
    replaces a response finalizer. Uvicorn resumes its normal send behavior once
    the peer is gone, allowing the response owner's finally to settle resources.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.prefix_sent = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.settled = asyncio.Event()
        self.release = asyncio.Event()
        self.status: int | None = None
        self.prefix = b""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        selected = scope["type"] == "http" and (b"x-fault-gate", b"disconnect") in scope["headers"]
        if not selected:
            await self.app(scope, receive, send)
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()

        async def pump() -> None:
            while True:
                message = await receive()
                await queue.put(message)
                if message["type"] == "http.disconnect":
                    self.disconnected.set()
                    return

        async def gated_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            if message["type"] == "http.response.body" and message.get("body") and not self.prefix:
                body = message["body"]
                self.prefix = body[:128]
                await send({**message, "body": self.prefix, "more_body": True})
                self.prefix_sent.set()
                await self.release.wait()
                await send({**message, "body": body[len(self.prefix) :]})
            else:
                await send(message)

        task = asyncio.create_task(pump())
        try:
            await self.app(scope, queue.get, gated_send)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.settled.set()


class _Server(uvicorn.Server):
    def __init__(self, app: Any) -> None:
        super().__init__(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                log_config=None,
                access_log=False,
                lifespan="on",
                ws="none",
                timeout_graceful_shutdown=1,
            )
        )
        self.ready = asyncio.Event()

    def capture_signals(self) -> Any:
        # The test host has no process signal ownership, including when embedded.
        return nullcontext()

    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets=sockets)
        self.ready.set()


@asynccontextmanager
async def live_listener(app: Any):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.setblocking(False)
    server = _Server(app)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await asyncio.wait_for(server.ready.wait(), 2)
        if not server.started:
            raise AssertionError("ASGI listener startup failed")
        yield f"http://127.0.0.1:{listener.getsockname()[1]}", server
    finally:
        if isinstance(app, DisconnectGate):
            app.release.set()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            listener.close()
