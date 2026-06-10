"""Manifest guardrail: pin the MCP server's public tool surface.

Builds the server (bound to a mock client) and lists its tools through the
in-memory FastMCP ``Client``, then pins:

* the EXACT set of tool names (the 25 of Phase 2a + 2b) — so a tool can't be
  silently added, removed, or renamed without updating this gate;
* a tool-count ceiling (28) leaving a little headroom over the ~25 design target;
* the ``destructiveHint`` annotation + a ``confirm`` parameter on every
  destructive (delete) tool; and
* the ``readOnlyHint`` annotation on every read-only tool.

Lives under ``tests/unit/mcp/`` so it is auto-skipped without the ``mcp`` extra
(see ``tests/unit/mcp/conftest.py``'s ``collect_ignore_glob``).
"""

from __future__ import annotations

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")


#: The complete, pinned tool surface. 25 tools across 7 domains. Adding or
#: removing a tool MUST update this set (and the ceiling below if it grows).
EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        # Notebooks (5)
        "notebook_list",
        "notebook_create",
        "notebook_describe",
        "notebook_rename",
        "notebook_delete",
        # Sources (6)
        "source_list",
        "source_get_content",
        "source_rename",
        "source_delete",
        "source_wait",
        "source_add",
        # Chat (2)
        "chat_ask",
        "chat_configure",
        # Notes (4)
        "note_create",
        "note_list",
        "note_update",
        "note_delete",
        # Artifacts (4)
        "artifact_list",
        "artifact_generate",
        "artifact_status",
        "artifact_download",
        # Research (3)
        "research_start",
        "research_status",
        "research_import",
        # Meta (1)
        "server_info",
    }
)

#: Tool-count ceiling. The design target is ~25; 28 leaves a little headroom so a
#: deliberate addition is a one-line bump, but an accidental explosion still
#: trips the gate.
TOOL_CEILING = 28

#: The three destructive tools — each carries ``destructiveHint`` AND a
#: ``confirm`` parameter (the both-mode confirmation contract).
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({"notebook_delete", "source_delete", "note_delete"})

#: Read-only tools — each carries ``readOnlyHint``.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "notebook_list",
        "notebook_describe",
        "source_list",
        "source_get_content",
        "note_list",
        "artifact_list",
        "artifact_status",
        "research_status",
        "server_info",
    }
)


@pytest.fixture
async def tools_by_name(mcp_list_tools):
    """Map of ``tool name -> Tool`` from the live server manifest."""
    tools = await mcp_list_tools()
    return {tool.name: tool for tool in tools}


async def test_exact_tool_set(tools_by_name) -> None:
    """The registered tool names equal the pinned frozenset, exactly."""
    actual = frozenset(tools_by_name)
    missing = EXPECTED_TOOLS - actual
    extra = actual - EXPECTED_TOOLS
    assert not missing, f"expected tools missing from the server: {sorted(missing)}"
    assert not extra, f"unexpected tools registered on the server: {sorted(extra)}"
    assert actual == EXPECTED_TOOLS


async def test_tool_count_within_ceiling(tools_by_name) -> None:
    """The tool count stays at/under the ceiling (catch an accidental explosion)."""
    assert len(tools_by_name) == len(EXPECTED_TOOLS)
    assert len(tools_by_name) <= TOOL_CEILING


@pytest.mark.parametrize("name", sorted(DESTRUCTIVE_TOOLS))
async def test_destructive_tools_annotated_and_confirmable(name, tools_by_name) -> None:
    """Every destructive tool carries destructiveHint AND a ``confirm`` param."""
    tool = tools_by_name[name]
    assert tool.annotations is not None, f"{name} has no annotations"
    assert tool.annotations.destructiveHint is True, f"{name} missing destructiveHint"
    assert tool.annotations.readOnlyHint is False, f"{name} must not be read-only"
    properties = tool.inputSchema.get("properties", {})
    assert "confirm" in properties, f"{name} must expose a 'confirm' parameter"


@pytest.mark.parametrize("name", sorted(READ_ONLY_TOOLS))
async def test_read_only_tools_annotated(name, tools_by_name) -> None:
    """Every read-only tool carries readOnlyHint (and is not destructive)."""
    tool = tools_by_name[name]
    assert tool.annotations is not None, f"{name} has no annotations"
    assert tool.annotations.readOnlyHint is True, f"{name} missing readOnlyHint"
    assert tool.annotations.destructiveHint is False, f"{name} must not be destructive"


async def test_read_only_and_destructive_are_disjoint() -> None:
    """Sanity: no tool is both read-only and destructive in the pinned sets."""
    assert not (READ_ONLY_TOOLS & DESTRUCTIVE_TOOLS)
    assert READ_ONLY_TOOLS <= EXPECTED_TOOLS
    assert DESTRUCTIVE_TOOLS <= EXPECTED_TOOLS
