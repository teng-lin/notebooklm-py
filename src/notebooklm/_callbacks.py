"""Internal helpers for user-supplied callbacks."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


async def maybe_await_callback(callback: Callable[..., Any], *args: Any) -> None:
    """Invoke a sync or async callback and await its result when needed."""
    result = callback(*args)
    if inspect.isawaitable(result):
        await result
