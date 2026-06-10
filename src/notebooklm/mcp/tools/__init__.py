"""MCP tool modules.

Each domain module exposes a single ``register(mcp)`` entry point that adds its
tools to the FastMCP server. :func:`notebooklm.mcp.server.register_all` calls
every module's ``register`` so the manifest has one chokepoint.

This package imports NO ``click`` / ``rich`` / ``cli`` — only ``fastmcp`` and the
``_app`` cores (enforced by ``tests/_guardrails/test_mcp_boundary.py``).
"""

from __future__ import annotations

__all__: list[str] = []
