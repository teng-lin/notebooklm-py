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
SCHEMA_CHAR_BUDGET = (
    39_400  # total serialized inputSchema + description chars (current 39_319; +81 slack)
)
# #1890 folded the two source-add composites BACK into source_add (ADR-0025 tool
# consolidation): the add+wait verb → source_add(wait=True, timeout=, interval=), and
# the in-channel byte upload → source_add(source_type="file", bytes_base64=, filename=).
# source_add grew to 15 params / ~4.9k chars for the merged surface, but removing
# source_add_and_wait + source_upload_bytes is a net −2 tools and −3_099 schema chars.
# Ratcheted DOWN from 42_450 to the new 39_319 actual (don't leave freed slack).
# Phase 1 (remote MCP file upload) added await_upload — the completion signal for a
# source_add(source_type="file") link, so the model learns the browser/mobile upload
# landed without the user narrating it. A NEW discrete tool (+773 chars, 2 params:
# upload_link, timeout), not growth of an existing one; per ADR-0025 the discrete verb
# is preferred over widening source_add, and the docstring is kept lean. It reuses the
# existing ADR-0024 signer + in-process jti store (no new state), so this is the whole
# schema cost. Raised from 41_700.
# #1884 added source_add_drive_file (auto-route add-from-Drive: download + upload the
# upload-only Drive types NotebookLM's native import can't ingest) — a NEW discrete
# tool (+1_144 chars, 4 params), not growth of an existing one. Per ADR-0025 the
# discrete verb is preferred over widening source_add(source_type="drive"), which
# REQUIRES an explicit mime_type (#1827) and only imports Google-native + PDF by
# reference; auto-download is a categorically different operation. Raised from 40_550.
# Prior baseline: current was 40_501, +37 for source_add's batch docstring documenting
# the #1871 fatal-abort semantics (a per-URL input failure isolates, a fatal service
# failure aborts the whole call).
# ^ Raised from 36_250 for #1741: research_status gained include_report /
# report_max_chars / source_limit / source_offset windowing params, and the four
# research tools' docstrings speak one `poll_task_id` id (tightened to stay lean).
# #1789 renamed research_status/import's `task_id` and research_cancel's `run_id`
# params to `poll_task_id` (keeping the old names as deprecated aliases → one extra
# param each), absorbed by trimming the now-redundant id-routing prose from those
# docstrings; measured full-surface cost is 36_934. The ceiling is a deliberately
# minimal buffer (ADR-0025) — trim before adding more.
# #1803 added source_upload_bytes (the in-channel small-file byte-upload for the
# network-boxed-agent dead-zone) — a NEW discrete tool (+1_581 chars, 5 params),
# not growth of an existing one; ADR-0025 prefers a discrete verb over widening the
# source_add mega-tool, whose schema-budget headroom this would otherwise consume.
# Measured full-surface cost is 38_572.
# #1807 added source_add_and_wait (single-mode source_add + source_wait composed into
# one call — the add→wait round-trip an agent otherwise makes itself) — another NEW
# discrete tool (~1_920 chars, 11 params), not growth of an existing one; same
# ADR-0025 discrete-verb rationale as source_upload_bytes. Measured full-surface cost
# is 40_412 (after the #1806 / #1805 rebase, which shifted the baseline).
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
