"""Poll the stored-auth MCP startup worker's published JSON report."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


async def observe_report(
    path: Path, predicate: Callable[[Any], Any], *, timeout: float = 10
) -> Any:
    """Return the first report state satisfying ``predicate`` within ``timeout``."""

    async def poll() -> Any:
        while True:
            try:
                contents = path.read_text(encoding="utf-8")
            except (FileNotFoundError, PermissionError):
                # The worker publishes via atomic replacement. Windows can deny
                # a racing open briefly; retry within the observation deadline.
                pass
            else:
                value = json.loads(contents)
                if predicate(value):
                    return value
            await asyncio.sleep(0.025)

    return await asyncio.wait_for(poll(), timeout)
