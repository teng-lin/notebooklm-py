# CLI business-logic-separation refactor — process log

**Status:** Planning
**Started:** 2026-06-08
**Branch:** `refactor/cli-business-logic` (worktree `.worktrees/cli-refactor`)
**Governing decision:** the relocation decision (a new ADR, written at the integration step)

## Goal
Refactor the CLI layer so **business logic is separated from presentation/transport
(Click/Rich/Console)** and is reusable by the CLI **and** other front-ends (MCP, future
HTTP). Make the reusable logic transport-neutral and importable without dragging Click in.

## Constraints (from the user)
1. **Preserve major command-line options** (removing/cleaning some options or commands is OK
   if it makes the surface cleaner — call it out).
2. **Preserve `--json` output** (the emitted envelope/shape is a de-facto user contract).
3. **Reuse existing cassettes — do NOT record new ones**, even to support new front-ends.
   (Cassettes match on `rpcids` + body shape, so changing the *code path* keeps them valid.)
4. **Separate business logic from non-business** so it can be reused.
5. May consult **oracle (Claude+Codex)** for architecture; **momus (Claude+Codex)** to review the plan.
6. Execute via a **workflow** with **parallel agents in separate worktrees, merged back**, each
   using **polish**. Record the whole process (this file).

## Survey findings (2026-06-08)
- Command groups (16): session, notebook, chat, doctor, source, artifact, agent, generate,
  download, note, label, share, skill, research, language, profile.
- `cli/services/`: 24 modules. Only **2 import Click/Console** (auth_source, session_context) —
  ADR-0008 mostly worked at the import level — BUT **15 carry signature-level coupling**
  (`json_output` flags, `raw_args`+`parameter_explicit`, `console`/`Console` seams).
- Big command modules (likely inline business logic): source_cmd 969, generate_cmd 881,
  artifact_cmd 756, session_cmd 752, chat_cmd 669, label_cmd 496, note_cmd 470.
- Cross-cutting utils, Click-coupled: `cli/resolve.py` (54 refs), `cli/rendering.py` (44),
  `cli/helpers.py` (11), `cli/error_handler.py`.
- Reuse/regression target: 15 `tests/integration/cli_vcr/test_*.py` cassette suites + the
  `--json` envelope-shape assertions.

## Plan of attack (meta-process)
1. **Survey workflow** (parallel, read-only) → per-group gap inventory.  ← running
2. **Oracle (Claude+Codex)** → target architecture + migration strategy.   ← running
3. Synthesize → **comprehensive plan**.
4. **Momus (Claude+Codex)** review → revise.
5. **Execution workflow**: parallel worktree agents (polished) per slice → merge back → integrate.
6. Finalize this log + summary.

### Log
- Phase 0: worktree + survey done; oracle + survey workflow dispatched.

## Planning inputs — DONE
- **Oracle (Claude + Codex), both independently → RELOCATE to `_app/`** (overturning the earlier in-place-utilities proposal's
  "in-place" for the broader reuse goal; both even chose the name `_app/`). Codex refinement:
  `_app` returns typed Result dataclasses; CLI keeps ALL `--json` envelope assembly.
- **Survey workflow (7 agents): ~70 extraction units** grounded to file:line across all groups.
- **Comprehensive plan written** → `docs/scratch/2026-06-08-cli-refactor-plan.md`.
- **Momus (Claude + Codex) dispatched** to review the plan against the code (pressure-testing
  strangler-wrappers-vs-patch-seams, --json byte-stability, cassette invariance, MCP-dedup litmus,
  prototype scope). ← running

## Architecture (decided)
RELOCATE business logic to `src/notebooklm/_app/`; CLI/MCP/HTTP = thin adapters; cli/services →
wrappers → deleted last. Contract: `build_<verb>_plan(Request)->Plan` (pure, typed errors) +
`async execute_<verb>(plan, client, progress) -> Result` (typed dataclass). CLI owns envelopes +
exit codes; `_app` is click/rich/cli/fastmcp-free (boundary lint). Strangler migration; cassettes
reused (matcher = rpcids+body-shape). Wave 0 foundation (serialized) → parallel domain waves →
auth last → integration. Supersedes the earlier in-place-utilities proposal on placement.

## Momus review — BOTH REJECT (convergent, code-grounded). Architecture endorsed; plan incomplete.
Critical fixes required (both reviewers):
1. Per-domain JSON SERIALIZER INVENTORY before extraction (download dict, listing.json_envelope:131,
   source clean _dispatch_source_clean_result:910, source research _render_add_research_result:533,
   generate result, artifact/note/label payloads). Canonical contract = typed Result dataclass; the
   EXISTENCE PROOF is source_clean->SourceCleanResult + source_research->SourceAddResearchResult
   (already conform). download is the COUNTER-example (returns dict) -> W1 converts it (NOT near-verbatim).
2. WRAPPER/PATCH-SEAM SEMANTICS: re-export wrappers DON'T preserve patch seams if _app closes over the
   symbol (repo documents this at conftest.py:483). Rule: patched symbols read at call-time/injected.
   NotebookLMClient seam is on the COMMAND module (untouched by moves - good). cli/helpers.py (101
   patches) is a serialization point; keep resolve_*/validate_id re-exports resolvable through it.
3. ERROR TAXONOMY SPLIT: _app.classify(exc)->(category,retriable); CLI + MCP keep their OWN code maps
   (not one table). CLI byte-stable codes != MCP shorter codes.
4. RESOLVER/RPC-ORDER preservation table per command (full-ID fast-path / resolved-in-cmd / -in-svc /
   passthrough). W1/W2 low risk (IDs resolved inside executors); W3 sources flagged.
5. MCP DEDUP in-scope: prototype replaces mcp/_serialize.py with _app import + byte-equiv test.
6. Prototype MUST include sources (clean/add-research) + golden --json characterization tests per domain.
   W1 must include download_helpers.py + _download_specs.py (dependency direction). 16 cli_vcr suites.
-> Revising plan to rev 2, then re-momus before execution.

## Re-momus on rev 2 — BOTH GO (Claude: OKAY-WITH-NITS; Codex: OKAY-WITH-NITS)
All 6 load-bearing fixes RESOLVED against live code. Claude confirmed the §4 wrapper/patch-seam
discipline is already demonstrated working in download_cmd/generate_cmd + the conftest.py:483 fixture.
Codex marked #3 PARTIAL (hygiene: MCP code list under-listed; ArtifactTimeoutError must stay
class-distinct) — amended into §5. Cosmetic nits (15 not 16 cli_vcr suites; ~656 not 644 patches;
real symbol names) fixed. **CLEARED FOR EXECUTION.**

## Execution — starting
Wave 0 (serialized): build src/notebooklm/_app/{errors(classify),serialize,resolve,events} + the
test_app_boundary lint + de-Click validate_id (wrapper discipline) + golden --json characterization
tests; full suite must stay green. Then parallel domain waves (download dict->dataclass; source
add-research) + the MCP _serialize dedup proof.

## Wave 0 — DONE (commit cb52bc14). Branch GREEN.
Built src/notebooklm/_app/{__init__,serialize,errors,resolve,events}.py (additive) + tests/unit/app/*
(127 tests) + tests/_guardrails/test_app_boundary.py + CLAUDE.md tree entry. Polished.
ErrorCategory (13, class-sensitive: ArtifactTimeoutError->ARTIFACT_TIMEOUT before WaitTimeoutError;
*NotFoundError->NOT_FOUND before RPCError catch-all; RPCTimeoutError->NETWORK). to_jsonable golden-tested
(enum-before-primitive). resolve: pure validate_id->ValidationError + click-free resolve_ref.
Doc-gate regression from the plan commit (unborn-ADR + bare module refs) fixed (c6d68bbc).
**Full suite: 8779 passed, 0 failed.**

## Workflow 2 — parallel domain waves (separate worktrees, polished, merged back)
- W1 download: dict execute_download -> typed DownloadResult in _app/download.py; CLI re-derives the
  exact --json envelope; include download_helpers.py + cli/_download_specs.py. Worktree .worktrees/cli-download.
- W-sources: source add-research logic -> _app; keep the 8-outcome command-layer dispatcher + exit codes.
  Worktree .worktrees/cli-source.
Each: keep patch seams (don't move command modules), cassettes reused, --json byte-stable, polish, merge back.

## Domain waves — DONE + integrated. Branch GREEN (8793 passed, 0 failed).
- W1 download (1d501a5b): `cli/services/download.py` 657→135 (thin adapter); new `_app/download.py`
  returns typed `DownloadResult` (discriminated by outcome) + `.to_envelope()` rebuilds the historical
  dict byte-for-byte. Patch seams preserved (download_cmd untouched; `services.download.resolve_notebook_id`
  read at call-time). Polish caught a real regression (dropped `require_notebook` env/active-context fallback) — fixed.
- W-sources (b186b628): `cli/services/source_research.py` 213→65; new `_app/source_research.py` holds the
  start→wait→import workflow + `validate_add_research_flags` (raises ValidationError, not UsageError);
  importer INJECTED. The 8-outcome `_render_add_research_result` dispatcher + exit codes stay in CLI.
- Merge conflict (both added the `_app` carve-out to test_cli_boundary): reconciled (kept one; covers both).
- Cassettes REUSED (no re-recording); `--json` byte-stable; mypy/ruff clean.

## MCP-dedup litmus — PROVEN (the payoff)
`tests/unit/app/test_app_serialize_mcp_equiv.py`: 16/16 — `_app.serialize.to_jsonable` is WIRE-byte-identical
to the MCP server's `mcp/_serialize.to_jsonable` across every type MCP emits. So MCP can delete its private
copy and `from .._app.serialize import to_jsonable` with zero behavior change. The actual file swap lands when
`feat/mcp-server` rebases onto `_app/` (a clean follow-up; this test is its green-light).

## PROTOTYPE COMPLETE
Wave 0 + W1 download + W-sources + MCP-dedup litmus — all green; full suite 8793 passed, 0 failed.
The `_app/` relocation is validated end-to-end on real CLI code AND proven reusable by a second front-end.
Remaining (follow-up, per plan): the other domain waves (artifacts/chat/notebook/note/label/research/auth),
the error_handler classify-routing rewire, the feat/mcp-server rebase + _ids/_errors dedup, the de-monkeypatch
pass (inject client via ctx.obj), and the relocation ADR. De-monkeypatch explicitly deferred per the user.
