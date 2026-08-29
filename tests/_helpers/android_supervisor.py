"""Real-supervisor fake transport for Android workflow lifecycle tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker

Handler = Callable[[Any, dict[str, Any]], Any]


class SupervisedAndroidTransport:
    """Dispatch fake unary calls through the production admission supervisor."""

    def __init__(self) -> None:
        self.supervisor = CallSupervisor(
            metrics=ClientMetrics(),
            drain_tracker=TransportDrainTracker(),
            max_concurrent_rpcs=None,
        )
        self.supervisor.set_bound_loop(asyncio.get_running_loop())
        self.supervisor.reset_after_open()
        self.supervisor.prepare_generation(1)
        self.supervisor.start_accepting(1)
        self.handlers: dict[str, Handler | Any] = {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def operation_scope(self, label: str, **kwargs: Any) -> Any:
        return self.supervisor.operation_scope(label, **kwargs)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        async with self.supervisor.call_scope(
            method,
            None,
            None,
            expected_epoch=kwargs.get("expected_epoch"),
        ):
            self.calls.append((method, request, kwargs))
            result = self.handlers[method]
            if callable(result):
                result = result(request, kwargs)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, BaseException):
                raise result
            return result

    async def force_close_and_reopen(self) -> Any:
        """Retire epoch one with work active and start epoch two."""
        old_generation = self.supervisor._current
        assert old_generation is not None and old_generation.epoch == 1
        await self.supervisor.begin_closing(1)
        self.supervisor.mark_closed(1)
        self.supervisor.reset_after_open()
        self.supervisor.prepare_generation(2)
        self.supervisor.start_accepting(2)
        return old_generation


__all__ = ["SupervisedAndroidTransport"]
