"""Offline MCP tool-surface eval harness (ADR-0025 tripwire).

Static, deterministic, no live model — it measures the two things a mock-client
harness *can* measure honestly and ratchets them so the surface can't silently
bloat:

* **schema-token cost** — a char proxy (serialized ``inputSchema`` + description
  length) per tool and surface-wide. Leaner descriptions / fewer params cost less
  agent context every call.
* **schema-ambiguity proxy** — per-tool visible param count. A tool with a huge
  param list is the "one tool mirrors N backend operations" smell (ADR-0025's
  mega-tool discussion). Ratcheting the max catches a `source_add` /
  `studio_generate` that grows further, or a new mega-tool.

Live tool-selection accuracy is intentionally NOT measured here (it needs a real
model); it is out of scope for this offline harness.

Run ``pytest tests/unit/mcp/test_tool_eval.py -s`` to see the per-tool table.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastmcp")


#: Ratchet ceilings — calibrated to the current surface (Tier-1 read-merge took it
#: to ~36.0k). Move these DOWN as the surface gets leaner; a rise means
#: description/param bloat that must be justified, not rubber-stamped.
SCHEMA_CHAR_BUDGET = 37_020  # total serialized inputSchema + description chars (current 36_979)
# ^ Raised from 36_250 for #1741: research_status gained include_report /
# report_max_chars / source_limit / source_offset windowing params, and the four
# research tools' docstrings speak one `poll_task_id` id (tightened to stay lean).
# Measured full-surface cost is 36_780 on this rebase (incl. #1747/#1749/#1755/#1748
# sharing confirm gates + the review-driven report_truncated/traceability wording);
# the ceiling is a deliberately minimal buffer (ADR-0025) — trim before adding more.
MAX_PARAMS_PER_TOOL = 22  # studio_generate is the current high-water mark


@pytest.fixture
async def tools_by_name(mcp_list_tools):
    """Map of ``tool name -> Tool`` from the live in-memory server manifest."""
    tools = await mcp_list_tools()
    return {tool.name: tool for tool in tools}


def _schema_chars(tool) -> int:
    schema = json.dumps(tool.inputSchema, sort_keys=True)
    return len(schema) + len(tool.description or "")


def _param_count(tool) -> int:
    # ``ctx`` is not a wire param; count only the advertised input properties.
    return len(tool.inputSchema.get("properties", {}))


async def test_surface_schema_cost_within_budget(tools_by_name) -> None:
    """Total serialized tool-schema cost stays under the ratchet."""
    total = sum(_schema_chars(t) for t in tools_by_name.values())
    assert total <= SCHEMA_CHAR_BUDGET, (
        f"tool-schema char cost {total} exceeds budget {SCHEMA_CHAR_BUDGET}; a tool's "
        "description/params grew — trim it or justify raising the budget (ADR-0025)."
    )


@pytest.mark.parametrize("name", ["studio_generate", "source_add"])
async def test_mega_tools_do_not_grow(name, tools_by_name) -> None:
    """The known mega-tools stay under the param ceiling (ADR-0025: don't split, don't grow)."""
    assert _param_count(tools_by_name[name]) <= MAX_PARAMS_PER_TOOL


async def test_no_tool_exceeds_param_ceiling(tools_by_name) -> None:
    """No tool exceeds the param ceiling — catches a NEW mega-tool sneaking in."""
    offenders = {n: _param_count(t) for n, t in tools_by_name.items()}
    over = {n: c for n, c in offenders.items() if c > MAX_PARAMS_PER_TOOL}
    assert not over, f"tools over the {MAX_PARAMS_PER_TOOL}-param ceiling: {over}"


async def test_print_eval_report(tools_by_name, capsys) -> None:
    """Emit the per-tool cost table (visible with ``-s``); not an assertion gate."""
    rows = sorted(
        ((n, _schema_chars(t), _param_count(t)) for n, t in tools_by_name.items()),
        key=lambda r: r[1],
        reverse=True,
    )
    total = sum(r[1] for r in rows)
    with capsys.disabled():
        print(f"\n=== MCP tool-eval ({len(rows)} tools, {total} schema-chars) ===")
        print(f"{'tool':<26}{'chars':>8}{'params':>8}")
        for name, chars, params in rows:
            print(f"{name:<26}{chars:>8}{params:>8}")
