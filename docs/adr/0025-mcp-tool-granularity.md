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

## Update (2026-07, #1890): fold the source-add composites back into `source_add`

> The **35/40** figures in the Context/Consequences above are **historical** —
> the surface as of this ADR's original authoring. Intervening additions (the
> sharing domain, `suggest_prompts`, `source_add_drive_file`, `source_upload_bytes`,
> `source_add_and_wait`, `await_upload`) took it to **36**; this update brings the
> surface to **34** (later **33** — see the #1896 update below).


Two source tools shipped as **discrete verbs** over the composite-vs-mega-tool
tension: `source_add_and_wait` (single-mode add + `source_wait` in one call) and
`source_upload_bytes` (in-channel base64 file-add). Neither is a distinct operation —
each is just a facet of adding a source: `source_upload_bytes` is a *file input mode*
(bytes instead of a path, decoded before the add runs), and `source_add_and_wait` is a
*same-call composition* of an add with the follow-on `source_wait` poll. On top of that,
`source_add_drive_file` already carried a `wait: bool`, making a separate wait-*verb* an
inconsistency. They were folded back into `source_add`:

- **add + wait** → `source_add(..., wait=True, timeout=…, interval=…)` — returns the
  `source_wait` aggregate + top-level `source_id`; single-source only, not for a remote
  `file` signed-URL upload.
- **in-channel bytes** → `source_add(source_type="file", bytes_base64=…, filename=…)` —
  the `file` alternative to `path`, on any transport.

Net **36 → 34 tools** and **−3,099 schema chars** (`SCHEMA_CHAR_BUDGET` ratcheted from
42,450 to 39,400). `source_add` grows to **15 params** — still well under
`MAX_PARAMS_PER_TOOL = 22`, and the `test_mega_tools_do_not_grow` param ceiling holds.
This is the "prefer overloading an existing tool over adding a new one" side of the
same fewer-tools evidence that argued *against* splitting the mega-tools: consolidating
these composites lowers the surface-wide schema-token cost the harness ratchets. The
underlying `_app` add+wait / bytes logic (`_waitagg`, `_fileupload`) is retained
verbatim — only the two MCP tool *registrations* were removed.


## Update (2026-07, #1896): fold `studio_get_prompt` into `studio_list`

> This update brings the current surface to **33**.

`studio_get_prompt(notebook, artifact)` was a discrete read-only tool returning one
artifact's generation prompt. But the typed `Artifact` already carries
`generation_prompt` (decoded from the `LIST_ARTIFACTS` row, #1925) and the default
`studio_list` **summary** listing already surfaces it on every artifact row — so the
standalone tool duplicated a capability the unified listing tool already had. It was
removed and its single-artifact lookup folded onto the existing `studio_list(item=…)`
path (which now threads `include_artifact_meta=True` so a resolved artifact carries its
prompt).

This is the "prefer folding into an existing tool over a standalone verb" side of the
ADR-0025 fewer-tools evidence — the same rationale as the #1890 `source_add` fold. The
prompt rides the `LIST_ARTIFACTS` row the listing already fetches, so there is **no extra
request** (no per-artifact fetch, no N+1).

Net **34 → 33 tools** and **−367 schema chars** (`SCHEMA_CHAR_BUDGET` ratcheted from
39,400 to 39,050; measured actual 39,015). The transport-neutral `_app.get_artifact_prompt`
core is retained verbatim — the CLI `notebooklm artifact get-prompt` and the REST route
still use it; only the MCP tool *registration* was removed.

**Resolution semantics note:** the `studio_list(item=…)` lookup resolves over the unified
cross-type Studio resolver (`resolve_studio_item`: full id / hex-prefix / exact title over
the merged notes+artifacts list — the same resolver `studio_delete` / `studio_rename` use),
NOT the old artifact-scoped `resolve_artifact`. So, versus `studio_get_prompt`: a title that
is exact-shared by both a note and an artifact resolves ambiguously (pass `kind` to scope),
and artifact-*title-prefix* lookups are not supported (use the id or full title). This is a
deliberate consequence of the one-resolver Studio surface — and moot for the common case,
since the summary listing already exposes every artifact's prompt without any ref.
