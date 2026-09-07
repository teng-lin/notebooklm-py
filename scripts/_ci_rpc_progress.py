"""Live RPC diagnostics on a separate stream from private response reports."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from typing import Any, ParamSpec, TextIO, TypeVar

if __package__:
    from ._ci_progress import open_progress_stream, write_summary
else:
    from _ci_progress import open_progress_stream, write_summary

P = ParamSpec("P")
T = TypeVar("T")

_SKIP_REASONS = {
    "Method always skipped (complex setup or quota)": "complex setup or quota; intentionally not probed",
    "Duplicate method (same ID as another)": "same RPC ID is checked by another method",
    "Requires real resource IDs (placeholder fails)": "requires real resource IDs",
    "Tested in setup/cleanup phases": "handled in setup/cleanup; see those phase results",
    "Requires --full mode (creates/deletes resources)": "requires full mode",
    "No test parameters available": "no safe probe parameters available",
    "No notebook ID provided": "notebook required for this probe",
}


def safe_reason(result: Any, proof: str, detail: str = "") -> str:
    """Classify errors without publishing their bodies or resource handles."""
    status = result.value if isinstance(result, Enum) else result.status.value
    error = getattr(result, "error", None) or getattr(result, "detail", "") or detail
    if status == "SKIPPED":
        return _SKIP_REASONS.get(error, "probe prerequisites unavailable")
    if status == "MISMATCH":
        observed = [
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{5,6}", value)
            else "<unexpected ID shape>"
            for value in result.found_ids[:10]
        ]
        return "expected RPC ID absent; observed IDs=" + (", ".join(observed) or "none")
    if "ReadTimeout" in error:
        return "RPC read timeout (transient)"
    if any(
        marker in error
        for marker in (
            "HTTP 429",
            "RateLimitError",
            "API rate limit",
            "Chat rate limit",
            "RESOURCE_EXHAUSTED",
        )
    ):
        return "quota/rate limit (transient)"
    http_status = re.search(r"\bHTTP (\d{3})\b", error)
    if http_status:
        return f"HTTP {int(http_status[1])}; see report"
    for category in ("ConnectTimeout", "WriteTimeout", "PoolTimeout", "ConnectError"):
        if category in error:
            return category
    if "Parse error" in error or "ParseError" in error:
        return "response decoding failed; see report"
    if status == "OK":
        if error:
            return "expected RPC ID observed, but operation rejected; see report"
        return proof
    return {
        "MATCH": "option tables match the checked-in enums",
        "DRIFT": "option-table drift; compare expected/observed values in report",
        "PRESENT": "endpoint response framing recognized",
        "ABSENT": "endpoint unavailable; see report",
        "UNAUTHENTICATED": "authentication rejected",
        "NOT_PROBED": "requires a separate authenticated capture",
        "CURRENT": "build label is current",
        "DRIFTED": "build label changed within the allowed age",
        "STALE": "pinned build label is stale; see report",
        "UNKNOWN": "probe inconclusive; see report",
    }.get(status, "probe failed; see full diagnostic report")


class RPCProgress:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.phase = "initialization"
        self.rows: list[tuple[str, str, str, float, str]] = []
        self.stream_failed = False

    def emit(self, message: str) -> None:
        if self.stream_failed:
            return
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        try:
            print(f"[{stamp}] {message}", file=self.stream, flush=True)
        except OSError:
            self.stream_failed = True
            print("WARNING: could not write live RPC progress", file=sys.stderr, flush=True)

    def finish(self, label: str, status: str, started: float, reason: str) -> None:
        elapsed = time.monotonic() - started
        self.rows.append((self.phase, label, status, elapsed, reason))
        self.emit(f"{self.phase}: {status} {label}; elapsed={elapsed:.1f}s; {reason}")

    def summarize(self) -> None:
        write_summary(
            "### RPC probe details\n\n"
            "RPC-ID OK means the expected response ID was observed; live E2E tests check behavior. "
            "Setup/cleanup entries report their own calls. Rebrand probes have a separate verdict.\n\n"
            "| Phase | Probe / RPC ID | Outcome | Seconds | Diagnostic |\n"
            "| --- | --- | --- | ---: | --- |"
        )
        for phase, label, status, elapsed, reason in self.rows:
            write_summary(f"| {phase} | `{label}` | {status} | {elapsed:.1f} | {reason} |")
        if not self.rows:
            write_summary(f"No RPC probe completed; last phase: {self.phase}.")


_ACTIVE: ContextVar[RPCProgress | None] = ContextVar("rpc_progress", default=None)


@contextmanager
def live_progress(fd: int | None) -> Iterator[None]:
    if fd is None:
        yield
        return
    with open_progress_stream(fd) as stream:
        progress = RPCProgress(stream)
        token = _ACTIVE.set(progress)
        try:
            yield
        finally:
            _ACTIVE.reset(token)
            progress.summarize()


def progress_phase(phase: str) -> None:
    progress = _ACTIVE.get()
    if progress is not None:
        progress.phase = phase
        progress.emit(f"RPC phase: {phase}")


def progress_note(message: str) -> None:
    progress = _ACTIVE.get()
    if progress is not None:
        progress.emit(message)


def trace_probe(
    label: str | None = None, *, proof: str = "expected RPC ID observed"
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Trace a checked result, including setup, inventory reads, and cleanup."""

    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            progress = _ACTIVE.get()
            if progress is None:
                return await function(*args, **kwargs)
            name = label
            if name is None:
                method = kwargs.get("method", args[2] if len(args) > 2 else None)
                name = f"RPC-ID {method.name} ({method.value})"
            started = time.monotonic()
            progress.emit(f"{progress.phase}: START {name}")
            try:
                value = await function(*args, **kwargs)
            except BaseException:
                progress.finish(name, "ERROR", started, "probe interrupted or raised; see report")
                raise
            result = value[0] if isinstance(value, tuple) else value
            status = result.value if isinstance(result, Enum) else result.status.value
            detail = value[1] if isinstance(value, tuple) and isinstance(value[1], str) else ""
            progress.finish(name, status, started, safe_reason(result, proof, detail))
            return value

        return wrapped

    return decorate
