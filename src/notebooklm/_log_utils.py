"""Logging utilities for safe payload handling."""

import json
from typing import Any


def truncate_payload(data: Any, max_len: int = 500) -> str:
    """Truncate payload for safe logging.

    Args:
        data: Any JSON-serializable data
        max_len: Maximum length before truncation (default 500)

    Returns:
        JSON string, truncated with byte count if exceeds max_len
    """
    s = json.dumps(data, separators=(",", ":"))
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... ({len(s)} bytes total)"
