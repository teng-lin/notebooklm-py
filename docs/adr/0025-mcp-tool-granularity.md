# ADR-0025: MCP tool granularity — mega-tools vs. discrete verbs

## Status

Accepted.

## Context

The MCP surface is **35 tools** (`tests/unit/mcp/test_manifest.py`, ceiling 40),
above the 5–15/server that current guidance recommends (Anthropic *Writing
effective tools for agents*, Sep 2025; GitHub cut Copilot 40→13 for measurable
accuracy + latency gains). A tool-interface review flagged that two tools carry
the opposite problem — they are **mega-tools** whose real contract lives in
runtime validators the JSON schema can't express:

- `source_add` — 10 params, two modes (single via `source_type`, batch via
  `urls`), every param optional; three runtime validators enforce which
  combinations are legal (`src/notebooklm/mcp/tools/sources.py`).
- `artifact_generate` — 20 params; per-kind option *applicability* is checked at
  runtime (the option *values* are already `Literal`s pinned to the core maps).

The "finish the discrete-verb direction" fix (ADR-0021's transport-neutral
philosophy applied to the tool boundary) would split these so the schema states
each contract. But splitting **raises the tool count**, which collides with the
"fewer tools" evidence — unless paired with **progressive disclosure** (deferred
tool loading), which cut schema tokens ~85% *and* raised accuracy in Anthropic's
Tool Search Tool.

The decisive constraint: progressive disclosure is a **client/platform** feature.
The MCP spec (2025-06-18) has the server advertise its whole tool list via
`tools/list`; there is **no server-forced deferred loading**. So an MCP server
cannot guarantee a lean in-context surface for arbitrary clients (Claude
Desktop/Code, Cursor, …). Ceiling math (**as of the Tier-1 read-merges, which took the
surface from 37 to 35**): splitting `source_add` into `source_add_url` / `_file` /
`_text` (keeping the existing batch mode) is **+3 tools = 38**, now within the 40
ceiling with a little headroom — so the ceiling no longer blocks *that* split by
itself; a full `artifact_generate` per-family split (+several) would still breach 40.
(At authoring time the surface was 37, making the `source_add` split land at exactly
40 — the Tier-1 merges since freed those two slots.)

## Decision

**Do not split the mega-tools now.** Specifically:

1. **`artifact_generate` stays unified.** Its finite options are already `Literal`
   enums pinned to the core maps; only per-kind applicability is runtime, and a
   per-family split would breach the ceiling and duplicate the shared
   `source_ids` / `language` / `style` params across N tools. Improve it instead
   via leaner docstrings + per-kind examples (the response-shaping phase).
2. **`source_add` split is deferred, not adopted.** It is the stronger candidate
   (mutually-exclusive params, three runtime validators) but it already batches
   and would consume all remaining ceiling headroom. Revisit only if (a) a
   client-supported lean-surface mechanism materializes, or (b) we deliberately
   raise the ceiling with that split as the justification.
3. **No progressive-disclosure implementation.** We cannot force it server-side.
   We keep descriptions lean (so clients that *do* defer pay less) and leave the
   option of a config that registers a core tool subset as future work, not a
   committed deliverable.

The consistency and response-shaping improvements that do NOT touch tool count
(uniform mutation envelope, identifier/naming consistency, list pagination,
bounded content reads) proceed independently of this decision.

## Consequences

- The surface stays at 35/40 with the two mega-tools intact (Tier-1 read-merges cut it from 37; the mega-tool decision here is unchanged); agents keep learning
  `source_add` / `artifact_generate` validity partly by failed calls (mitigated by
  the leaner docstrings + examples).
- We avoid a count-inflating refactor we cannot pay for with deferred loading.
- If Anthropic/other clients standardize server-hintable deferred loading (several
  MCP SEPs are in flight), this decision should be revisited — the `source_add`
  split is the first thing to reconsider, with the ceiling raised as its rationale.
- The offline tool-eval harness (schema-token cost + param-count proxy) is the
  tripwire: if either mega-tool grows, or the surface-wide token cost creeps up,
  the ratchet fails and forces a fresh look.
