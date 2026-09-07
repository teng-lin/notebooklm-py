"""Keep MCP stdio output owned until the native transport finishes shutting down."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .mcp_startup_cleanup import _finish


def _describe_message(message: Any) -> dict[str, Any]:
    root = getattr(getattr(message, "message", None), "root", None)
    item: dict[str, Any] = {
        "kind": type(root).__name__ if root is not None else type(message).__name__
    }
    request_id = getattr(root, "id", None)
    if isinstance(request_id, int):
        item["request_id"] = request_id
    method = getattr(root, "method", None)
    if method is not None:
        item["method"] = (
            method
            if method
            in {
                "notifications/tools/list_changed",
                "notifications/resources/list_changed",
                "notifications/prompts/list_changed",
                "notifications/message",
                "notifications/progress",
                "ping",
            }
            else "other"
        )
    return item


@asynccontextmanager
async def stdio_session(params: Any, directory: Path, errlog: Any):
    """Drain late protocol messages after ClientSession closes, without dropping errors.

    ClientSession closes its receive endpoint before stdio_client closes child
    stdin and waits for process exit. A separate receiver must remain alive
    during that interval, including after stdio_client closes its own endpoint.
    Otherwise a valid late message makes the SDK sender raise BrokenResourceError.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    primary: BaseException | None = None
    failures: list[tuple[str, BaseException]] = []
    drain_task: asyncio.Task | None = None
    shutdown_reader = None
    late: list[dict[str, Any]] = []
    late_count = 0

    async def drain() -> None:
        nonlocal late_count
        async for message in shutdown_reader:
            late_count += 1
            if isinstance(message, BaseException):
                failures.append(("late_protocol", message))
            if len(late) >= 20:
                continue
            late.append(_describe_message(message))

    try:
        async with stdio_client(params, errlog=errlog) as (reader, writer):
            shutdown_reader = reader.clone()
            try:
                async with ClientSession(reader.clone(), writer) as session:
                    try:
                        yield session
                    except BaseException as error:
                        # Keep the exact body error while every native owner exits.
                        primary = error
            except BaseException as error:
                failures.append(("session", error))
            finally:
                drain_task = asyncio.create_task(drain())
    except BaseException as error:
        failures.append(("transport", error))
    finally:
        if drain_task is not None:
            try:
                await asyncio.wait_for(drain_task, 2)
            except BaseException as error:
                failures.append(("drain", error))
        if shutdown_reader is not None:
            try:
                await shutdown_reader.aclose()
            except BaseException as error:
                failures.append(("receiver_close", error))
        if primary is not None or failures:
            logging.getLogger(__name__).error(
                "MCP stdio shutdown: primary=%s late=%s cleanup=%s",
                None if primary is None else type(primary).__name__,
                late,
                [{"step": step, "error_type": type(error).__name__} for step, error in failures],
            )
        _finish(
            directory,
            "stdio",
            failures,
            primary,
            late_message_count=late_count,
            late_messages=late,
            drainer_settled=drain_task is None or drain_task.done(),
        )
    if primary is not None:
        raise primary
