"""Manifest guardrail: pin the MCP server's public tool surface.

Builds the server (bound to a mock client) and lists its tools through the
in-memory FastMCP ``Client``, then pins:

* the EXACT set of tool names — so a tool can't be silently added, removed, or
  renamed without updating this gate;
* a tool-count ceiling (40): the current surface is 35 tools; the next tool
  stays under the ceiling, but an accidental explosion still trips the gate;
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


#: The complete, pinned tool surface. 35 tools across 8 domains. Adding or
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
        "source_read",
        "source_rename",
        "source_delete",
        "source_wait",
        "source_add",
        # Chat (3)
        "chat_ask",
        "chat_configure",
        "suggest_prompts",
        # Notes (4)
        "note_create",
        "note_list",
        "note_update",
        "note_delete",
        # Artifacts (8)
        "artifact_list",
        "artifact_generate",
        "artifact_status",
        "artifact_get_prompt",
        "artifact_download",
        "artifact_rename",
        "artifact_retry",
        "artifact_delete",
        # Research (4)
        "research_start",
        "research_status",
        "research_import",
        "research_cancel",
        # Sharing (4)
        "share_status",
        "share_set_access",
        "share_set_user",
        "share_remove_user",
        # Meta (1)
        "server_info",
    }
)

#: Tool-count ceiling. The design target is ~25; the sharing domain (#1684) took
#: the surface to 34, the artifact get-prompt/retry tools took it to 36, and
#: suggest_prompts to 37; the Tier-1 read-merges (source_describe+source_get_content
#: → source_read, note_get → note_list(note?)) brought it to 35. The ceiling has
#: headroom, but an accidental explosion still trips the gate.
TOOL_CEILING = 40

#: The destructive tools — each carries ``destructiveHint`` AND a ``confirm``
#: parameter (the both-mode confirmation contract).
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {"notebook_delete", "source_delete", "note_delete", "artifact_delete", "share_remove_user"}
)

#: Read-only tools — each carries ``readOnlyHint``.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "notebook_list",
        "notebook_describe",
        "source_list",
        "source_read",
        "note_list",
        "artifact_list",
        "artifact_status",
        "artifact_get_prompt",
        "research_status",
        "share_status",
        "suggest_prompts",
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


async def test_artifact_rename_is_plain_mutating_tool(tools_by_name) -> None:
    """``artifact_rename`` mutates but is neither read-only nor destructive.

    A title-only update carries default annotations (no ``readOnlyHint``, no
    ``destructiveHint``) and no ``confirm`` gate — so it must stay out of both the
    read-only and destructive pinned sets.
    """
    assert "artifact_rename" in tools_by_name
    assert "artifact_rename" not in READ_ONLY_TOOLS
    assert "artifact_rename" not in DESTRUCTIVE_TOOLS
    tool = tools_by_name["artifact_rename"]
    if tool.annotations is not None:
        assert not tool.annotations.readOnlyHint
        assert not tool.annotations.destructiveHint
    assert "confirm" not in tool.inputSchema.get("properties", {})


async def test_artifact_retry_is_plain_mutating_tool(tools_by_name) -> None:
    """``artifact_retry`` mutates but is neither read-only nor destructive.

    Kicking off a retry carries default annotations (no ``readOnlyHint``, no
    ``destructiveHint``) and no ``confirm`` gate — so it must stay out of both the
    read-only and destructive pinned sets.
    """
    assert "artifact_retry" in tools_by_name
    assert "artifact_retry" not in READ_ONLY_TOOLS
    assert "artifact_retry" not in DESTRUCTIVE_TOOLS
    tool = tools_by_name["artifact_retry"]
    if tool.annotations is not None:
        assert not tool.annotations.readOnlyHint
        assert not tool.annotations.destructiveHint
    assert "confirm" not in tool.inputSchema.get("properties", {})


async def test_artifact_download_advertises_artifact_id_and_format_enum(tools_by_name) -> None:
    """``artifact_download`` advertises the ``artifact_id`` param and an enumerated
    ``output_format`` so an agent's tool schema can target a specific artifact and
    pick a valid format (issue #1668)."""
    import json

    tool = tools_by_name["artifact_download"]
    properties = tool.inputSchema.get("properties", {})
    assert "artifact_id" in properties, "artifact_download must expose 'artifact_id'"
    assert "artifact" in properties, "artifact_download must expose the 'artifact' name-or-id ref"
    assert "output_format" in properties, "artifact_download must expose 'output_format'"
    # output_format is a Literal union → the schema (possibly under anyOf for the
    # optional ``| None``) must enumerate every supported format value.
    fmt_schema = json.dumps(properties["output_format"])
    for value in ("pdf", "pptx", "json", "markdown", "html"):
        assert value in fmt_schema, f"output_format schema missing {value!r}: {fmt_schema}"
    # ``artifact_type`` is now optional (target by ``artifact`` ref instead) but must
    # still advertise its full type enum so the by-type path stays schema-guided.
    type_schema = json.dumps(properties["artifact_type"])
    for value in ("audio", "video", "slide-deck", "quiz", "flashcards"):
        assert value in type_schema, f"artifact_type schema missing {value!r}: {type_schema}"
