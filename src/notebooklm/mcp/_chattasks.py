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
blessed ephemeral in-process state): single process / single tenant, with all
operations serialized by the server event loop and no lock. The ``start`` claim
contains no ``await``, so duplicate submissions remain atomic; explicit cancel
waits only after it has found and cancelled the claimed task. The registry is
bounded two ways — a hard entry cap with oldest-done-first eviction, and a
completion TTL — so a long-lived server cannot leak memory. It dies with the
process, like the lifespan client it rides on.

In-flight dedupe only: entries are keyed by a stable hash of the ask's semantic
inputs (:func:`compute_chat_task_key`), so a retry of a question that is still
generating attaches to the running task (no double generation, and a
``chat_start`` response the transport dropped is recovered by re-issuing it).
A FINISHED entry never satisfies a new ``chat_start``: ``client.chat.ask``
appends a turn to a conversation, so asking twice must produce two turns —
exactly like ``chat_ask`` — and a replayed answer would silently go stale the
moment a source is added or ``chat_configure`` runs. Finished entries stay in
the registry only so ``chat_status(task_id)`` can serve their payload for the
completion TTL.

This module imports NO ``click`` / ``rich`` / ``cli`` / ``fastmcp`` — pure
stdlib, so the tool layer owns all MCP-facing shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from .._loop_bound import LoopBoundPrimitive

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
#: Default ceiling on generations IN FLIGHT against Google. Accepted asks past
#: this queue (FIFO by submission) and auto-start as slots free up, so callers
#: can submit a whole batch of questions at once and just poll — the server owns
#: the pacing. Kept deliberately small: bursts of concurrent generations on one
#: shared Google account have empirically triggered account-level throttling.
#: Override with the ``NOTEBOOKLM_MCP_CHAT_CONCURRENCY`` env var (clamped 1-16).
_DEFAULT_CONCURRENCY = 3
_CONCURRENCY_ENV = "NOTEBOOKLM_MCP_CHAT_CONCURRENCY"


def _resolve_concurrency() -> int:
    """Return the generation-concurrency ceiling (env-overridable, clamped 1-16)."""
    raw = os.environ.get(_CONCURRENCY_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_CONCURRENCY
    return max(1, min(16, value))


#: How long a finished entry (result or error) stays claimable after completion.
#: Long enough for a model to come back across several re-invoke cycles (and for
#: a user to retry a question whose response the transport dropped); short
#: enough that answers never linger for hours in memory.
_RESULT_TTL_S = 30.0 * 60.0


class ChatTaskCapacityError(RuntimeError):
    """Raised by :meth:`ChatTaskRegistry.start` when the registry is full of
    UNFINISHED work (the retained-entry fuse; queueing normally absorbs bursts).
    The tool layer maps this onto its validation vocabulary."""


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
    created_at: float  # time.monotonic(); submission time
    notebook_id: str | None = None
    conversation_id: str | None = None
    #: The server-owned task. Assigned immediately after construction (the entry
    #: must exist first so ``_guard`` can stamp it); ``None`` only in that gap.
    task: asyncio.Task[None] | None = None
    #: time.monotonic() when the generation actually began (concurrency slot
    #: acquired); ``None`` while queued. ``started_at - created_at`` is the queue
    #: wait, ``done_at - started_at`` the generation time — surfaced by
    #: ``chat_status`` so real-world concurrency can be calibrated from timings.
    started_at: float | None = None
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    done_at: float | None = None  # time.monotonic(); None while running
    #: time.time() at completion — the TTL clock. Monotonic time is deliberately
    #: NOT used for expiry: observed live on a gVisor-sandboxed host (bunny
    #: Magic Containers, 2026-09-02), the sandbox's monotonic clock effectively
    #: freezes while the container idles, so monotonic-based TTLs let results
    #: outlive their window by wall-hours. Durations (``queued_s`` /
    #: ``generation_s``) stay monotonic — they span actively-running code.
    done_wall: float | None = None


class ChatTaskRegistry(LoopBoundPrimitive):
    """Bounded, TTL-swept map of detached chat asks (see module docstring).

    Carries the #1196 loop-affinity protocol (via :class:`LoopBoundPrimitive` +
    :meth:`reset_after_open`): the MCP lifespan binds the registry to its running
    loop, and a later loop rebind rebuilds the semaphore and drops unfinished
    entries (their tasks died with the old loop; completed results are plain
    dicts and survive).
    """

    def __init__(
        self,
        *,
        max_tasks: int = _MAX_TASKS,
        concurrency: int | None = None,
        result_ttl_s: float = _RESULT_TTL_S,
    ) -> None:
        self._max_tasks = max_tasks
        self._concurrency = concurrency if concurrency is not None else _resolve_concurrency()
        #: Gates generations against Google: accepted asks past the ceiling wait
        #: here (FIFO) and auto-start as slots free — the server owns the pacing.
        self._gate = asyncio.Semaphore(self._concurrency)
        self._result_ttl_s = result_ttl_s
        self._tasks: dict[str, ChatTaskEntry] = {}
        self._by_key: dict[str, str] = {}

    @property
    def concurrency(self) -> int:
        """The generation-concurrency ceiling this registry paces to."""
        return self._concurrency

    def _on_loop_rebind(self, old: Any, new: Any) -> None:
        # Loop-bound state cannot cross loops: rebuild the gate and drop
        # unfinished entries (their tasks belong to the old loop).
        self._gate = asyncio.Semaphore(self._concurrency)
        for entry in [e for e in self._tasks.values() if e.done_at is None]:
            self._drop(entry)

    def reset_after_open(self) -> None:
        """Per-open reset (the #1196 affinity protocol). The registry keeps no
        per-open counters; the rebind hook owns the state discard."""

    # -- internals ---------------------------------------------------------

    def _unlink(self, entry: ChatTaskEntry) -> None:
        # Only unlink the key if it still points at THIS entry (a finished
        # entry's key may already have been re-claimed by a fresh start).
        if self._by_key.get(entry.key) == entry.task_id:
            del self._by_key[entry.key]

    def _drop(self, entry: ChatTaskEntry) -> None:
        self._tasks.pop(entry.task_id, None)
        self._unlink(entry)

    def _expired(self, entry: ChatTaskEntry, now_wall: float) -> bool:
        return entry.done_wall is not None and (now_wall - entry.done_wall) > self._result_ttl_s

    def _sweep(self, now_wall: float) -> None:
        for entry in [e for e in self._tasks.values() if self._expired(e, now_wall)]:
            self._drop(entry)

    def _running_count(self) -> int:
        return sum(1 for e in self._tasks.values() if e.done_at is None)

    def counts(self) -> dict[str, int]:
        """Live registry gauges for observability (``server_info``): how many
        asks are ``generating`` (slot held) vs ``queued`` (waiting for one),
        plus ``cached_results`` and the pacing ``concurrency`` ceiling."""
        self._sweep(time.time())
        generating = sum(
            1 for e in self._tasks.values() if e.done_at is None and e.started_at is not None
        )
        queued = sum(1 for e in self._tasks.values() if e.done_at is None and e.started_at is None)
        cached = sum(
            1 for e in self._tasks.values() if e.done_at is not None and e.result is not None
        )
        return {
            "generating": generating,
            "queued": queued,
            "concurrency": self._concurrency,
            "cached_results": cached,
        }

    async def _guard(
        self, entry: ChatTaskEntry, coro_factory: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        """Drive one ask to its terminal state, recording the outcome.

        The generation waits for a concurrency slot first (the queue), and the
        coroutine is only CREATED once the slot is held — a task cancelled while
        queued never instantiates (and so never leaks) the underlying ask.
        ``CancelledError`` (explicit generation cancellation or server shutdown)
        is recorded then re-raised, per asyncio's cancellation contract.
        """
        try:
            async with self._gate:
                entry.started_at = time.monotonic()
                entry.result = await coro_factory()
        except asyncio.CancelledError:
            entry.error = asyncio.CancelledError("chat task cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - terminal outcome capture, projected at read time
            entry.error = exc
        finally:
            if entry.result is None and entry.error is None:
                # Only reachable while a non-Exception BaseException (SystemExit /
                # KeyboardInterrupt) propagates — never report a bare "completed".
                entry.error = RuntimeError("chat task ended without a result")
            entry.done_at = time.monotonic()
            entry.done_wall = time.time()
            # A finished key is free again: the next chat_start for it re-asks.
            self._unlink(entry)

    # -- API ---------------------------------------------------------------

    def start(
        self,
        key: str,
        coro_factory: Callable[[], Awaitable[dict[str, Any]]],
        *,
        notebook_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[ChatTaskEntry, Literal["created", "running"]]:
        """Claim ``key`` and return its entry plus how it was satisfied.

        * ``"running"`` — an identical ask is already in flight; attach to it
          (no double generation).
        * ``"created"`` — a fresh server-owned task was accepted. It generates
          immediately if a concurrency slot is free, else queues (FIFO) and
          auto-starts as slots free up — callers submit whole batches and just
          poll; the server owns the pacing. A FINISHED entry for the key —
          result or error — never short-circuits: asking again re-asks (two
          asks are two conversation turns, as with ``chat_ask``; a replayed
          answer would be stale after ``source_add`` / ``chat_configure``). The
          finished entry stays pollable by ``task_id`` for the TTL.

        No ``await`` between the existence check and the spawn — on the single
        server loop the claim is atomic, so concurrent duplicate calls cannot
        both spawn.

        Raises:
            ChatTaskCapacityError: every retained slot holds UNFINISHED work
                (the fuse; the queue absorbs normal bursts long before this).
        """
        now = time.monotonic()
        self._sweep(time.time())
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            entry = self._tasks.get(existing_id)
            if entry is not None:
                if entry.done_at is None:
                    return entry, "running"
                # Finished (result or error): fall through and spawn a fresh ask.
        if len(self._tasks) >= self._max_tasks:
            # Evict oldest DONE entries to make room; unfinished tasks are never
            # evicted (their work is paid for).
            done = sorted(
                (e for e in self._tasks.values() if e.done_at is not None),
                key=lambda e: e.done_wall or 0.0,
            )
            for entry in done[: max(1, len(self._tasks) - self._max_tasks + 1)]:
                self._drop(entry)
            if len(self._tasks) >= self._max_tasks:
                raise ChatTaskCapacityError(
                    f"all {self._max_tasks} task slots hold unfinished asks; "
                    "poll chat_status and let the queue drain before starting more"
                )
        task_id = secrets.token_urlsafe(8)
        entry = ChatTaskEntry(
            task_id=task_id,
            key=key,
            created_at=now,
            notebook_id=notebook_id,
            conversation_id=conversation_id,
        )
        # Spawn AFTER the entry exists so ``_guard`` can stamp it; a plain loop
        # task (not a child of the request scope) is the detachment guarantee.
        # The factory is handed over uncalled — the ask coroutine is created
        # only once a concurrency slot is held (see ``_guard``).
        entry.task = asyncio.create_task(self._guard(entry, coro_factory))
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
        if self._expired(entry, time.time()):
            self._drop(entry)
            return None
        return entry

    async def cancel(self, task_id: str) -> bool:
        """Cancel one unfinished local ask task and wait for it to unwind.

        Returns ``True`` only when a live task was cancelled. Unknown and
        already-terminal task IDs are harmless no-ops.
        """
        entry = self.status(task_id)
        if entry is None or entry.done_at is not None or entry.task is None:
            return False
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)
        if entry.done_at is None:
            # Cancellation before the task's first scheduler step never enters
            # ``_guard``; stamp it here so status and TTL remain truthful.
            entry.error = asyncio.CancelledError("chat task cancelled")
            entry.done_at = time.monotonic()
            entry.done_wall = time.time()
            self._unlink(entry)
        return True

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
        now_wall = time.time()
        for entry in running:
            if entry.done_at is None:
                # A task cancelled before its first scheduler step never entered
                # ``_guard`` (the coroutine body never ran), so nothing stamped
                # the entry — stamp it here (both clocks, so it can expire) so no
                # entry outlives ``aclose`` in a perpetually-"running" state.
                entry.error = asyncio.CancelledError("chat task cancelled at server shutdown")
                entry.done_at = now
                entry.done_wall = now_wall
                self._unlink(entry)
