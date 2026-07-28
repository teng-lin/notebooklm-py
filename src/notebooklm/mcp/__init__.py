"""MCP server for notebooklm-py (opt-in ``mcp`` extra).

A transport-neutral MCP adapter that sits beside ``cli/`` over the ``_app/``
business-logic layer. ``from notebooklm.mcp import create_server`` builds a
FastMCP server driving a single long-lived
:class:`~notebooklm.client.NotebookLMClient`; run it with the ``notebooklm-mcp``
console script (stdio or loopback HTTP).

This package imports NO ``click`` / ``rich`` / ``cli`` — it is built on the
``_app/`` cores only (enforced by ``tests/_guardrails/test_mcp_boundary.py``).
"""

from __future__ import annotations

import os as _os

# FastMCP's Settings() (pulled in transitively by the .server import below) bool-parses
# FASTMCP_STATELESS_HTTP at import time and RAISES on an empty string — exactly what a
# docker-compose `${FASTMCP_STATELESS_HTTP:-}` passthrough injects when the operator hasn't set it.
# Treat an empty value as unset so a blank env var can't crash-loop the server before it even
# reaches argv/flag handling (the widget-driven stateless default is applied later, in __main__).
_stateless = _os.environ.get("FASTMCP_STATELESS_HTTP")
if _stateless is not None and _stateless.strip() == "":  # "" or whitespace-only → treat as unset
    del _os.environ["FASTMCP_STATELESS_HTTP"]

from .server import SERVER_INSTRUCTIONS, SERVER_NAME, create_server  # noqa: E402 — guard runs first

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "create_server"]
