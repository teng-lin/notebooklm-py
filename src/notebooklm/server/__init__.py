"""Single-tenant REST API server over the transport-neutral ``_app`` layer.

**Experimental.** Like the ``mcp/`` adapter, this package is experimental: the
``/v1`` surface and behavior may change in a minor release. It ships behind the
optional ``server`` extra and is excluded from the public-API compatibility
gate. Pin a version before relying on it for automation.

The third adapter (after ``cli/`` and ``mcp/``, per ADR-0021): a
localhost HTTP surface that lets scripts and agents drive NotebookLM without a
CLI process per call. v1 exposes notebooks, sources, chat, and studio-artifact
generation / download; long-running work uses a poll-the-resource model; every
``/v1`` request requires a bearer token; failures project from
``_app.errors.classify`` to an HTTP status.

It ships behind an optional ``server`` extra and launches via its own
``notebooklm-server`` console script. This package imports NO ``click`` /
``rich`` / ``notebooklm.cli`` (enforced by
``tests/_guardrails/test_server_boundary.py``); importing it without the
``server`` extra installed fails on the ``fastapi`` import.
"""

from __future__ import annotations

from .app import SERVER_NAME, create_app

__all__ = ["SERVER_NAME", "create_app"]
