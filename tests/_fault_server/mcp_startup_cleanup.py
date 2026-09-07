"""Independent cleanup attempts for the isolated stored-auth MCP probes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from notebooklm._atomic_io import atomic_write_json


def _finish(
    directory: Path,
    lane: str,
    failures: list[tuple[str, BaseException]],
    primary: BaseException | None,
    **evidence: Any,
) -> None:
    try:
        atomic_write_json(
            directory / f"{lane}-cleanup.json",
            {
                "failures": [
                    {"step": step, "error_type": type(error).__name__} for step, error in failures
                ],
                "primary_error": None if primary is None else type(primary).__name__,
                **evidence,
            },
        )
    except BaseException as error:
        failures.append(("report", error))
    if primary is None and failures:
        raise failures[0][1]


async def settle_http_worker(process: Any, directory: Path, primary: BaseException | None) -> None:
    """A failed graceful stop cannot skip forced termination or replace a failure."""
    failures: list[tuple[str, BaseException]] = []
    try:
        (directory / "stop").touch()
    except BaseException as error:
        failures.append(("stop", error))
    try:
        await asyncio.wait_for(process.wait(), 5)
    except BaseException as error:
        failures.append(("graceful_wait", error))
    if process.returncode is None:
        try:
            process.kill()
        except BaseException as error:
            failures.append(("kill", error))
        try:
            await asyncio.wait_for(process.wait(), 2)
        except BaseException as error:
            failures.append(("forced_wait", error))
    _finish(directory, "worker", failures, primary, exit_code=process.returncode)


async def settle_calls_and_upstream(
    calls: list[asyncio.Task],
    upstream: Any,
    directory: Path,
    primary: BaseException | None,
    *,
    timeout: float = 2,
) -> None:
    """Release the gate and close its owner even when caller settlement fails."""
    failures: list[tuple[str, BaseException]] = []
    try:
        upstream.release("opening")
    except BaseException as error:
        failures.append(("release", error))
    for call in calls:
        if not call.done():
            call.cancel()
    try:
        await asyncio.wait_for(upstream.aclose(), timeout)
    except BaseException as error:
        failures.append(("upstream_close", error))
    pending = set()
    if calls:
        try:
            done, pending = await asyncio.wait(calls, timeout=timeout)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                for task in pending:
                    task.cancel()
                failures.append(("caller_settlement", TimeoutError()))
                done_again, pending = await asyncio.wait(pending, timeout=timeout)
                for task in done_again:
                    if not task.cancelled():
                        task.exception()
        except BaseException as error:
            failures.append(("caller_settlement", error))
    # A cancelled wait may never assign its return tuple; observe live tasks now.
    for task in calls:
        if task.done() and not task.cancelled():
            task.exception()
    _finish(
        directory,
        "upstream",
        failures,
        primary,
        pending_callers=sum(not task.done() for task in calls),
        active_handlers=upstream.active_handlers,
    )
