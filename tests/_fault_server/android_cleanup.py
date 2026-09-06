"""Bounded Android cohort cleanup with primary-outcome preserving evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from .common import ScenarioFailure, ScenarioResult


async def settle_actions(actions: list[Callable[[], Awaitable[Any]]]) -> list[BaseException]:
    """Attempt every owned close despite failures or repeated caller cancellation."""
    failures: list[BaseException] = []

    async def attempt(action: Callable[[], Awaitable[Any]]) -> BaseException | None:
        try:
            await action()
        except BaseException as exc:
            return exc
        return None

    for action in actions:
        closing = asyncio.create_task(asyncio.wait_for(attempt(action), 2))
        while True:
            try:
                error = await asyncio.shield(closing)
                if error is not None:
                    failures.append(error)
                break
            except asyncio.CancelledError as exc:
                failures.append(exc)
                if closing.done():
                    try:
                        settled_error = closing.result()
                    except BaseException as settled_error:
                        failures.append(settled_error)
                    else:
                        if settled_error is not None:
                            failures.append(settled_error)
                    break
            except BaseException as exc:
                failures.append(exc)
                break
    return failures


def finish_cleanup(
    result: ScenarioResult,
    primary: BaseException | None,
    failures: list[BaseException],
    *,
    clean: bool,
    **evidence: Any,
) -> None:
    result.record(
        "cleanup",
        **evidence,
        primary_error=None if primary is None else type(primary).__name__,
        cleanup_error_types=[type(exc).__name__ for exc in failures],
    )
    check_error: ScenarioFailure | None = None
    try:
        result.require("cleanup", clean and not failures)
    except ScenarioFailure as exc:
        check_error = exc
    for error in failures:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise error
    if primary is None:
        if failures:
            raise failures[0]
        if check_error is not None:
            raise check_error


@asynccontextmanager
async def android_cohort(
    result: ScenarioResult,
    rpc: Any,
    server: Any,
    factory: Callable[[], Any],
    *,
    release: Callable[[], None],
    tasks: list[asyncio.Task[Any]],
) -> AsyncIterator[Any]:
    """Open and settle a real client and both listeners, including partial startup."""
    harness = None
    primary: BaseException | None = None
    try:
        await rpc.__aenter__()
        await server.__aenter__()
        harness = factory()
        await harness.client.__aenter__()
        yield harness
    except BaseException as exc:
        primary = exc
        raise
    finally:
        pipeline = None if harness is None else harness.client._android_runtime.upload_pipeline
        assets = None if harness is None else harness.client._android_runtime.asset_downloads
        retained_files = () if pipeline is None else tuple(pipeline._open_files)
        retained_tasks = () if pipeline is None else tuple(pipeline._transport_tasks)
        retained_clients = () if pipeline is None else tuple(pipeline._transport_clients)
        if assets is not None:
            retained_tasks += tuple(assets._tasks)
            retained_clients += tuple(assets._clients)
        release()
        for task in tasks:
            if not task.done():
                task.cancel()
        actions: list[Callable[[], Awaitable[Any]]] = []
        if tasks:

            async def settle_tasks() -> None:
                await asyncio.gather(*tasks, return_exceptions=True)

            actions.append(settle_tasks)
        if harness is not None:
            actions.append(lambda: harness.client.close(drain=False))
        actions.extend([server.aclose, lambda: rpc.__aexit__(None, None, None)])
        failures = await settle_actions(actions)
        client_closed = harness is None or not harness.client._lifecycle.is_open()
        owners_settled = (
            all(item.closed for item in retained_files)
            and all(item.done() for item in retained_tasks)
            and all(item.is_closed for item in retained_clients)
        )
        finish_cleanup(
            result,
            primary,
            failures,
            clean=not server.active_handlers
            and not rpc._active
            and client_closed
            and owners_settled
            and not server.errors
            and not rpc.handler_errors
            and all(task.done() for task in tasks),
            handlers=server.active_handlers,
            rpc_handlers=len(rpc._active),
            client_closed=client_closed,
            pending_tasks=sum(not task.done() for task in tasks),
            remaining_http_actions=server.remaining(),
            remaining_rpc_actions=sum(len(actions) for actions in rpc.actions.values()),
            retained_files=len(retained_files),
            retained_transport_tasks=len(retained_tasks),
            retained_http_clients=len(retained_clients),
            retained_owners_settled=owners_settled,
            http_error_count=len(server.errors),
            rpc_error_count=len(rpc.handler_errors),
        )
