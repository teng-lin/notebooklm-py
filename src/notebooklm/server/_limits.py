"""Route-group concurrency limiters for the REST server.

The server can receive many simultaneous local requests across configured profiles.
These semaphores bound expensive route groups without putting cheap reads or
``/healthz`` behind a process-wide lock.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from .._adapter_support import LoopBoundPrimitive
from .._loop_affinity import assert_bound_loop

__all__ = [
    "DEFAULT_CHAT_CONCURRENCY",
    "DEFAULT_DOWNLOAD_CONCURRENCY",
    "DEFAULT_GENERATION_CONCURRENCY",
    "DEFAULT_RESEARCH_CONCURRENCY",
    "DEFAULT_SOURCE_MUTATION_CONCURRENCY",
    "DEFAULT_SOURCE_WAIT_CONCURRENCY",
    "LIMIT_ENV_VARS",
    "LimitGroup",
    "ServerLimiters",
]

LimitGroup = Literal[
    "source_mutation",
    "source_wait",
    "generation",
    "download",
    "research",
    "chat",
]

SOURCE_MUTATION_CONCURRENCY_ENV = "NOTEBOOKLM_SERVER_SOURCE_MUTATION_CONCURRENCY"
SOURCE_WAIT_CONCURRENCY_ENV = "NOTEBOOKLM_SERVER_SOURCE_WAIT_CONCURRENCY"
GENERATION_CONCURRENCY_ENV = "NOTEBOOKLM_SERVER_GENERATION_CONCURRENCY"
DOWNLOAD_CONCURRENCY_ENV = "NOTEBOOKLM_SERVER_DOWNLOAD_CONCURRENCY"
RESEARCH_CONCURRENCY_ENV = "NOTEBOOKLM_SERVER_RESEARCH_CONCURRENCY"
CHAT_CONCURRENCY_ENV = "NOTEBOOKLM_SERVER_CHAT_CONCURRENCY"

DEFAULT_SOURCE_MUTATION_CONCURRENCY = 4
DEFAULT_SOURCE_WAIT_CONCURRENCY = 4
DEFAULT_GENERATION_CONCURRENCY = 2
DEFAULT_DOWNLOAD_CONCURRENCY = 2
DEFAULT_RESEARCH_CONCURRENCY = 2
DEFAULT_CHAT_CONCURRENCY = 4

LIMIT_ENV_VARS: dict[LimitGroup, tuple[str, int]] = {
    "source_mutation": (SOURCE_MUTATION_CONCURRENCY_ENV, DEFAULT_SOURCE_MUTATION_CONCURRENCY),
    "source_wait": (SOURCE_WAIT_CONCURRENCY_ENV, DEFAULT_SOURCE_WAIT_CONCURRENCY),
    "generation": (GENERATION_CONCURRENCY_ENV, DEFAULT_GENERATION_CONCURRENCY),
    "download": (DOWNLOAD_CONCURRENCY_ENV, DEFAULT_DOWNLOAD_CONCURRENCY),
    "research": (RESEARCH_CONCURRENCY_ENV, DEFAULT_RESEARCH_CONCURRENCY),
    "chat": (CHAT_CONCURRENCY_ENV, DEFAULT_CHAT_CONCURRENCY),
}


@dataclass
class ServerLimiters(LoopBoundPrimitive):
    """Lifespan-owned semaphores for expensive REST route groups."""

    source_mutation_limit: int
    source_wait_limit: int
    generation_limit: int
    download_limit: int
    research_limit: int
    chat_limit: int
    _source_mutation: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _source_wait: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _generation: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _download: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _research: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _chat: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerLimiters:
        """Build route-group limiters from ``NOTEBOOKLM_SERVER_*`` env vars.

        Unset or blank values keep the conservative defaults. Invalid values fail
        during ASGI lifespan startup, before the app accepts traffic.
        """
        source = os.environ if env is None else env
        return cls(
            source_mutation_limit=_positive_int(source, "source_mutation"),
            source_wait_limit=_positive_int(source, "source_wait"),
            generation_limit=_positive_int(source, "generation"),
            download_limit=_positive_int(source, "download"),
            research_limit=_positive_int(source, "research"),
            chat_limit=_positive_int(source, "chat"),
        )

    @asynccontextmanager
    async def acquire(self, group: LimitGroup) -> AsyncIterator[None]:
        """Hold the semaphore for ``group`` for the duration of a route handler."""
        async with self._semaphore_for(group):
            yield

    def _semaphore_for(self, group: LimitGroup) -> asyncio.Semaphore:
        assert_bound_loop(self._bound_loop)
        if group == "source_mutation":
            if self._source_mutation is None:
                self._source_mutation = asyncio.Semaphore(self.source_mutation_limit)
            return self._source_mutation
        if group == "source_wait":
            if self._source_wait is None:
                self._source_wait = asyncio.Semaphore(self.source_wait_limit)
            return self._source_wait
        if group == "generation":
            if self._generation is None:
                self._generation = asyncio.Semaphore(self.generation_limit)
            return self._generation
        if group == "download":
            if self._download is None:
                self._download = asyncio.Semaphore(self.download_limit)
            return self._download
        if group == "research":
            if self._research is None:
                self._research = asyncio.Semaphore(self.research_limit)
            return self._research
        if group == "chat":
            if self._chat is None:
                self._chat = asyncio.Semaphore(self.chat_limit)
            return self._chat
        raise ValueError(f"Unknown limit group: {group}")

    def reset_after_open(self) -> None:
        """Discard loop-bound semaphores after a lifespan loop bind/rebind."""
        self._source_mutation = None
        self._source_wait = None
        self._generation = None
        self._download = None
        self._research = None
        self._chat = None

    def _on_loop_rebind(
        self,
        _old: asyncio.AbstractEventLoop | None,
        _new: asyncio.AbstractEventLoop | None,
    ) -> None:
        self.reset_after_open()


def _positive_int(env: Mapping[str, str], group: LimitGroup) -> int:
    name, default = LIMIT_ENV_VARS[group]
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer >= 1; got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be an integer >= 1; got {raw!r}")
    return value
