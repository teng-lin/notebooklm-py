"""Live-CI reporting for caller-validated, resource-free diagnostics.

Callers must supply only owned labels, counts and allowlisted status values;
credential scrubbing alone cannot make upstream exception bodies safe to publish.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def safe_test_name(node_id: str) -> str:
    """Keep source identifiers, excluding parametrized resource handles."""
    name = node_id.split("[", 1)[0]
    if re.fullmatch(r"tests/e2e/(?:\w+/)*test_\w+\.py(?:::\w+)*", name):
        return name
    return "unavailable test name"


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(markdown + "\n")
        except OSError:
            # Reporting must never replace the command's original failure.
            print("WARNING: could not write CI step summary", file=sys.stderr, flush=True)


def report(message: str, *, summary: bool = False, error: bool = False) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{timestamp}] {message}", file=sys.stderr if error else sys.stdout, flush=True)
    if summary or error:
        write_summary(f"- {message}")
    if error and os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{escaped}", flush=True)
