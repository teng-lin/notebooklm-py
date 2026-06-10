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

from .server import SERVER_INSTRUCTIONS, SERVER_NAME, create_server

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "create_server"]
