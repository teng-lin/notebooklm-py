"""In-process registry of detached chat asks (``chat_start`` / ``chat_status``).

Remote MCP transports enforce short per-call watchdogs — claude.ai's remote
connector cuts a tool call at ~60s of time-to-first-response-byte (the same
watchdog ``tools/_fileupload.py`` documents and designs around for uploads), and
progress notifications do not reset it. A blocking ``chat_ask`` whose NotebookLM
generation runs past the watchdog is therefore cancelled mid-flight, and because
request cancellation propagates into the handler's scope, the in-flight
``client.chat.ask`` dies with it — nothing persists, and the same question in
the NotebookLM web UI (which just waits) succeeds.

This module supplies the missing piece: a **server-owned task** that detaches
the ask from the request lifecycle. ``chat_start`` claims a slot here and spawns
the ask with :func:`asyncio.create_task` — a plain loop task, deliberately NOT a
child of the request's cancellation scope, so the transport killing the start
call cannot kill the generation. ``chat_status`` (a cheap re-invokable poll, the
load-bearing completion mechanism per the ADR-0024 upload precedent) reads the
completed payload from the registry.

Contract mirrors :class:`~notebooklm.mcp._filelink.ConsumedJtiStore` (ADR-0024's
blessed ephemeral in-process state): single process / single tenant; every
mutating method is fully synchronous and contains NO ``await``, so it runs
atomically w.r.t. other coroutines on the one server event loop and needs no
lock. The registry is bounded two ways — a hard entry cap with oldest-done-first
eviction, and a completion TTL — so a long-lived server cannot leak memory. It
dies with the process, like the lifespan client it rides on.

Idempotency: entries are keyed by a stable hash of the ask's semantic inputs
(:func:`compute_chat_task_key`). A retry of the same question attaches to the
in-flight task (no double generation) or returns the cached completed payload
instantly — which also converts a transport's dropped *response* (the host
discards results that arrive after its watchdog fired) into a cheap successful
retry.

This module imports NO ``click`` / ``rich`` / ``cli`` / ``fastmcp`` — pure
stdlib, so the tool layer owns all MCP-facing shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "ChatTaskCapacityError",
    "ChatTaskEntry",
    "ChatTaskRegistry",
    "compute_chat_task_key",
]

#: Hard ceiling on retained entries (running + completed). Generous for a
#: single-tenant server — one entry per distinct question — and swept by TTL;
#: the cap only guards a pathological long-lived server. Oldest *done* entries
#: are evicted first; running tasks are never evicted (see ``start``).
_MAX_TASKS = 256
#: Ceiling on concurrently RUNNING asks. Each one holds a live generation
#: round-trip against the shared Google account; past this the server refuses
#: new starts (:class:`ChatTaskCapacityError`) rather than queueing unbounded
#: work it cannot bound in time.
_MAX_RUNNING = 16
#: How long a finished entry (result or error) stays claimable after completion.
#: Long enough for a model to come back across several re-invoke cycles (and for
#: a user to retry a question whose response the transport dropped); short
#: enough that answers never linger for hours in memory.
_RESULT_TTL_S = 30.0 * 60.0


class ChatTaskCapacityError(RuntimeError):
    """Raised by :meth:`ChatTaskRegistry.start` when the running-task ceiling is
    reached. The tool layer maps this onto its validation vocabulary."""


def compute_chat_task_key(
    notebook_id: str,
    question: str,
    source_ids: list[str] | None,
    conversation_id: str | None,
    references: str,
) -> str:
    """Return the stable idempotency key for one semantic ask.

    Keyed on the *resolved* inputs (canonical notebook id + verbatim question +
    sorted resolved source ids + explicit conversation id + reference
    projection), so a retry of the same call dedupes regardless of how the
    caller originally spelled the notebook or source refs. Any semantic change —
    including the projection, which shapes the stored payload — is a different
    key and a fresh ask. ``\\x00`` separators keep adjacent fields from
    concatenation collisions.
    """
    parts = (
        notebook_id,
        question,
        ",".join(sorted(source_ids)) if source_ids else "",
        conversation_id or "",
        references,
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass
class ChatTaskEntry:
    """One detached ask: the server-owned task plus its terminal outcome.

    Exactly one of ``result`` / ``error`` is set once ``done_at`` is stamped;
    both stay ``None`` while the ask is in flight. ``error`` holds the raw
    exception — the tool layer projects it through the shared MCP error
    vocabulary at read time, so this module stays transport-neutral.
    """

    task_id: str
    key: str
    created_at: float  # time.monotonic()
    #: The server-owned task. Assigned immediately after construction (the entry
    #: must exist first so ``_guard`` can stamp it); ``None`` only in that gap.
    task: asyncio.Task[None] | None = None
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    done_at: float | None = None  # time.monotonic(); None while running


class ChatTaskRegistry:
    """Bounded, TTL-swept map of detached chat asks (see module docstring)."""

    def __init__(
        self,
        *,
        max_tasks: int = _MAX_TASKS,
        max_running: int = _MAX_RUNNING,
        result_ttl_s: float = _RESULT_TTL_S,
    ) -> None:
        self._max_tasks = max_tasks
        self._max_running = max_running
        self._result_ttl_s = result_ttl_s
        self._tasks: dict[str, ChatTaskEntry] = {}
        self._by_key: dict[str, str] = {}

    # -- internals ---------------------------------------------------------

    def _drop(self, entry: ChatTaskEntry) -> None:
        self._tasks.pop(entry.task_id, None)
        # Only unlink the key if it still points at THIS entry (a failed entry's
        # key may have been re-claimed by a fresh start).
        if self._by_key.get(entry.key) == entry.task_id:
            del self._by_key[entry.key]

    def _expired(self, entry: ChatTaskEntry, now: float) -> bool:
        return entry.done_at is not None and (now - entry.done_at) > self._result_ttl_s

    def _sweep(self, now: float) -> None:
        for entry in [e for e in self._tasks.values() if self._expired(e, now)]:
            self._drop(entry)

    def _running_count(self) -> int:
        return sum(1 for e in self._tasks.values() if e.done_at is None)

    async def _guard(self, entry: ChatTaskEntry, coro: Awaitable[dict[str, Any]]) -> None:
        """Drive one ask to its terminal state, recording the outcome.

        ``CancelledError`` (server shutdown via :meth:`aclose` — nothing else
        cancels these tasks) is recorded then re-raised, per asyncio's
        cancellation contract.
        """
        try:
            entry.result = await coro
        except asyncio.CancelledError:
            entry.error = asyncio.CancelledError("chat task cancelled at server shutdown")
            raise
        except BaseException as exc:  # noqa: BLE001 - terminal outcome capture, projected at read time
            entry.error = exc
        finally:
            entry.done_at = time.monotonic()

    # -- API ---------------------------------------------------------------

    def start(
        self,
        key: str,
        coro_factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> tuple[ChatTaskEntry, Literal["created", "running", "completed"]]:
        """Claim ``key`` and return its entry plus how it was satisfied.

        * ``"running"`` — an identical ask is already in flight; attach to it
          (no double generation).
        * ``"completed"`` — an identical ask finished successfully within the
          TTL; the cached payload answers instantly.
        * ``"created"`` — a fresh server-owned task was spawned. A previous
          FAILED entry for the key is replaced (a retry after an error must
          re-ask, mirroring "failures are not cached" idempotency semantics).

        No ``await`` between the existence check and the spawn — on the single
        server loop the claim is atomic, so concurrent duplicate calls cannot
        both spawn.

        Raises:
            ChatTaskCapacityError: the running-task ceiling is reached.
        """
        now = time.monotonic()
        self._sweep(now)
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            entry = self._tasks.get(existing_id)
            if entry is not None:
                if entry.done_at is None:
                    return entry, "running"
                if entry.result is not None:
                    return entry, "completed"
                # Failed within TTL: fall through and replace with a fresh ask.
        if self._running_count() >= self._max_running:
            raise ChatTaskCapacityError(
                f"{self._max_running} chat tasks are already running; "
                "poll chat_status for existing tasks before starting more"
            )
        if len(self._tasks) >= self._max_tasks:
            # Evict oldest DONE entries to make room; running tasks are never
            # evicted (their work is paid for — refusing new starts is the
            # pressure valve, via the running ceiling above).
            done = sorted(
                (e for e in self._tasks.values() if e.done_at is not None),
                key=lambda e: e.done_at or 0.0,
            )
            for entry in done[: max(1, len(self._tasks) - self._max_tasks + 1)]:
                self._drop(entry)
        task_id = secrets.token_urlsafe(8)
        entry = ChatTaskEntry(task_id=task_id, key=key, created_at=now)
        # Spawn AFTER the entry exists so ``_guard`` can stamp it; a plain loop
        # task (not a child of the request scope) is the detachment guarantee.
        entry.task = asyncio.create_task(self._guard(entry, coro_factory()))
        self._tasks[task_id] = entry
        self._by_key[key] = task_id
        return entry, "created"

    def status(self, task_id: str) -> ChatTaskEntry | None:
        """Return the entry for ``task_id``, or ``None`` if unknown/expired.

        An entry past its completion TTL is dropped and reported as ``None`` —
        the caller's remedy (start the ask again) is identical to the
        never-existed case, so the two are deliberately indistinguishable.
        """
        entry = self._tasks.get(task_id)
        if entry is None:
            return None
        if self._expired(entry, time.monotonic()):
            self._drop(entry)
            return None
        return entry

    async def aclose(self) -> None:
        """Cancel every running ask and wait them out (lifespan teardown).

        Runs before the lifespan client closes so no detached task touches a
        closing client. Completed entries are simply dropped with the process.
        """
        running = [e for e in self._tasks.values() if e.done_at is None and e.task is not None]
        for entry in running:
            assert entry.task is not None  # filtered above; narrows for mypy
            entry.task.cancel()
        if running:
            await asyncio.gather(*(e.task for e in running if e.task), return_exceptions=True)
        now = time.monotonic()
        for entry in running:
            if entry.done_at is None:
                # A task cancelled before its first scheduler step never entered
                # ``_guard`` (the coroutine body never ran), so nothing stamped
                # the entry — stamp it here so no entry outlives ``aclose`` in a
                # perpetually-"running" state.
                entry.error = asyncio.CancelledError("chat task cancelled at server shutdown")
                entry.done_at = now
