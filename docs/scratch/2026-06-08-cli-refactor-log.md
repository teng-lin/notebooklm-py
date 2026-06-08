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

## Sources wave (W3) — dispatching. (MCP feat/ merge DEFERRED per user; dedup litmus stays.)
Per-module verdict (business->_app, presentation stays; coupling/typed-grounded):
- MOVE: source_add(0), source_clean(0, typed), source_content(0), source_wait(1), source_listing(5 de-Click), source_mutations(14 de-Click, hardest).
- STAYS in cli/: source_serializers (the --json envelope builders = adapter half).
- DONE: source_research.
Dispatch: Agent A (near-verbatim trio add/clean/content), Agent B (de-Click trio wait/listing/mutations). Parallel worktrees, polished, merged back.

## Sources wave — DONE + integrated. Branch GREEN (8809 passed, 0 failed).
- Agent A (7615770a): source_add/clean/content → _app (near-verbatim) + re-export wrappers. SourceAddResult.payload
  inlines the source-summary dict (byte-verified) since _app can't import cli source_serializers.
- Agent B (4f8700cd): source_wait/listing/mutations → _app (de-Clicked); presentation (SourceMutationError render,
  --json envelopes, spinners, source_serializers) stays in cli; resolvers/confirmer/wait_context INJECTED;
  resolve_source_id monkeypatch seam preserved via call-time lookup. Codex polish: no Critical/Important.
- Merge: additive conflicts in _app/__init__ (reconstructed clean) + CLAUDE.md (combined rows) — resolved.
- **SOURCE DOMAIN COMPLETE: 7/8 services in _app/ (add/clean/content/listing/mutations/research/wait).
  source_serializers stays in cli/ (the --json envelope half).**

## Status: domains relocated so far = source (complete) + download (W1). All green, cassettes reused, --json byte-stable.
## Remaining domain waves (same proven recipe, parallelizable): generate, artifacts, chat, notebook/note/label/share/
## research, language/doctor, session/auth/profile (last). Plus: error_handler classify-routing; feat/mcp-server rebase
## + _ids/_errors dedup (deferred); de-monkeypatch via ctx.obj (deferred); relocation ADR at integration.

## Full parallel fan-out — all remaining domains at once (user: "launch them all in parallel")
6 agents in separate worktrees, polished, merged back:
1. generate (generate_cmd + services/generate*, artifact_generation)  -> .worktrees/dom-generate
2. artifacts (artifact_cmd)                                            -> .worktrees/dom-artifacts
3. chat (chat_cmd)                                                     -> .worktrees/dom-chat
4. crud: notebook+note+label+share (+ label_listing)                  -> .worktrees/dom-crud
5. misc: research+language+doctor+skill+agent                         -> .worktrees/dom-misc
6. session+auth+profile (HEAVIEST discipline — entry-assembler seam)  -> .worktrees/dom-auth
Shared (stays in cli/, agents must NOT move): listing.py, confirming_mutation.py, source_serializers,
rendering.py, helpers.py, resolve.py, error_handler.py, notebooklm_cli.py. Conflicts expected only in
_app/__init__ + CLAUDE.md (additive; reconciled at merge).

## Review of _app/source_add.py → 2 consistency wrinkles (added §11):
(1) SourceAddValidationError is a bare ValueError (outside notebooklm.exceptions → classify misses it; source_research did it right with ValidationError).
(2) SourceAddResult.payload builds the --json dict IN _app (re-introduces envelope-in-_app + duplicates source_serializers).
Action: added §11 consistency-pass; SendMessage to the 5 in-flight agents to raise public exceptions + not put .payload on _app results.

## Full fan-out — ALL 5 DOMAIN WAVES DONE (committed, not yet merged). Auth held; cleanup still running.
Each: own worktree, polished, full domain gate green, §11 constraints applied mid-flight via SendMessage
(only `ValidationError`-subclass exceptions in `_app`; no `.payload`/envelope-building in `_app`; command
modules NOT moved → NotebookLMClient patch seams intact). All cassettes REUSED; `--json` byte-stable.
- **artifacts** (`refactor/dom-artifacts` 1f85b16f): `_app/artifacts.py` (get/rename kind-aware/delete/export/
  poll/wait/retry + ArtifactStatusView). §11-clean.
- **chat** (`refactor/dom-chat` 6a629052): `_app/chat.py` (validate_ask_flags/determine_conversation_id/
  execute_configure/fetch_history). Fixed ChatValidationError→public ValidationError; envelope-builders→CLI;
  caught a real short-circuit bug (lazy `get_current_*` reads). `_app/chat.py` 100% covered.
- **misc** (`refactor/dom-misc` 53c3afb0): `_app/{skill,language,doctor,research}.py` (skill install,
  LanguageConfigStore+SUPPORTED_LANGUAGES, DoctorService.run_checks→DoctorReport, research wait/status).
  `agent show` correctly STAYED presentation (no headless caller). §11-clean.
- **generate** (`refactor/dom-generate` 46c02ccc): `_app/{generate_plans(753),generate_retry(286),
  generate(268)}.py` — split 3 ways to stay under the ADR-0008 900-line budget (mirrors the original CLI
  service trio). GenerationPlanValidationError→ValidationError; classify-coverage guardrail auto-discovers it.
  Polish removed GenerationOutcome from the cli/services/generate re-export surface (byte-identical surface).
- **crud** (`refactor/dom-crud` e6f422c4): `_app/{notebooks,notes,labels,sharing}.py`. Preserved: #1247
  BREAKING NOT_FOUND/exit-1 note path; label resolve ordering (`<id|name>` + LabelResolutionError→ValidationError,
  re-exported through label_listing for the 14 seam tests); share `set_view_level` keys notebook_id off the
  RESOLVED id (not status.notebook_id). Codex polish: Critical/Important/Minor = 0.

## Integration sequence (decided; executes when cleanup lands)
Conflict topology: 5 domains add DISJOINT `_app/<domain>.py` + DISJOINT CLI command edits; collide only on
ADDITIVE shared files (`_app/__init__.py`, `CLAUDE.md` `_app/` tree, maybe `test_services_boundary` GUARDED_PATHS).
Cleanup is different in KIND — it EDITS the existing `_app/source_*.py`/`download.py` + CLI adapters +
`source_serializers.py` (no domain touches those), so it's conflict-free vs domains EXCEPT where it also
edits the source/download rows of `__init__`/CLAUDE.md (real edits/removals, not additive).
Therefore:
1. Merge the 5 domains first. Reconcile `_app/__init__`/CLAUDE.md/GUARDED_PATHS ONCE as an additive UNION.
2. Merge `cleanup-migrated` LAST — take its source/download authority on the existing files + the
   `__init__`/CLAUDE.md source/download rows (NOT a blind keep-ours).
3. Safety net (fails loud if the union missed anything): `check_claude_md_freshness` (tree↔files),
   `test_app_boundary` (no click/rich/cli/fastmcp in `_app`), an `_app`-package import smoke-test,
   `module_size_ratchet`, then the full ~8,800-test suite + mypy + ruff.
4. THEN dispatch the held **auth** wave (`refactor/dom-auth` worktree) off the updated integration HEAD —
   heaviest discipline (entry-assembler `has_env_auth_json` seam).
5. Final integration: route `error_handler` through `_app.classify`; write the relocation ADR.
Deferred (per user): feat/mcp-server rebase + delete mcp/_serialize|_ids|_errors; de-monkeypatch via ctx.obj.

## INTEGRATION DONE — 5 domains + cleanup merged. Branch GREEN (8839 passed, 71 skipped, 1 xfailed, 0 failed).
Merge order: cleanup-migrated (clean, disjoint) → generate (auto-merged both files) → artifacts/chat/misc/crud.
Conflict surface was EXACTLY the two predicted additive files (`_app/__init__.py`, `CLAUDE.md`); every other
touched file single-editor → clean auto-merge. Resolution: hand-built the canonical `_app/__init__.py` union
(190 exports, all 6 domains, single `resolve`, cleanup's `source_summary` preserved) — ruff confirmed it was
already import-sorted; `checkout --ours` for crud's __init__ (canonical superset). CLAUDE.md `_app/` tree +
cli/services rows unioned by hand. §11 doc-consistency: corrected the now-stale `download.py`/`source_add.py`
rows (cleanup removed `to_envelope`/`.payload` → CLI builds envelopes from typed results via
`serialize.source_summary`). One guardrail catch: `test_public_docs_do_not_recommend_private_module_imports`
flagged a literal private-import string in THIS log → reworded.
Gate: freshness OK (251 paths); mypy clean (237 files); ruff clean; `_app` import smoke (190 names resolve);
test_app_boundary + module_size_ratchet green; full suite 8839 passed / 0 failed.
Domains now relocated to `_app/`: artifacts, chat, doctor, download, generate(+plans/retry), labels,
language, notebooks, notes, research, sharing, skill, source_* (8). **Only session/auth/profile remain.**

## NEXT: auth wave (session/auth/profile) — LAST domain, heaviest discipline. DISPATCHED (background, off HEAD 8a3fc8aa).

## RECOGNIZED PHASE (was missed; user flagged): test → `_app/` migration — see `docs/scratch/2026-06-08-cli-test-app-migration-map.md`.
The domain waves moved the LOGIC to `_app/` but the TESTS stayed in `tests/unit/cli/` (correct call — kept the
CLI patch seams intact during relocation). Net effect verified 2026-06-08 on the integrated branch: only 5 app
test files (artifacts/errors/resolve/serialize/serialize_mcp_equiv, 145 tests); **20 of 25 `_app` modules have NO
direct test**; `cli/resolve.py` still carries its own parallel `validate_id`/`resolve_partial_id_in_items`
(lines 43/192) with ~40 duplicate tests. The map's analysis still holds.
**Decision (user, 2026-06-08): run the WHOLE phase AFTER auth merges (not parallel), with FULL coverage scope —
all ~20 uncovered `_app` modules get direct tests.** Phase contents (map's recommended sequencing):
1. Free MOVEs (~65 already-pure tests, import retarget → `tests/unit/app/`; flagship: test_download_multi_artifact).
2. Net-new direct app tests for every uncovered module (doctor/chat first — zero coverage today — then the rest).
3. Resolve consolidation: collapse `cli/resolve.py`'s parallel impl into `_app/resolve.py`, delete ~40 dup tests
   — CAREFUL: 29 `cli.resolve.validate_id`/`cli.helpers.validate_id` patch seams must stay live (wrapper re-raises
   ValidationError→ClickException per plan §4). This is the one behavior-touching item, hence after-auth + careful.
4. Opportunistic SPLITs as each command is touched.
Sequenced after auth specifically because #3 edits shared `cli/resolve.py` (auth's session_context uses resolve).

## Domain relocation COMPLETE (auth merged) — integration GREEN: 8839 passed / 0 failed; 28 `_app` modules; 209 exports.
Auth merged conflict-free (integration hadn't touched __init__/CLAUDE.md since auth's branch point). New modules:
`_app/{session,auth_check,profile}.py`. Polish caught a real Critical on auth: adapter resolved `get_context_path`
EAGERLY before logout teardown vs the legacy LAZY-only-on-failure timing → fixed (LogoutInputs.context_path is a
lazy Callable, invoked only on the OSError branch). Every CLI command area's business logic now lives in `_app/`.

## Test-migration phase M1 — DISPATCHED (5 parallel cluster agents, off HEAD 3587a151).
Additive test work is fully FILE-DISJOINT (each agent owns a distinct set of cli `test_*.py` + creates distinct
`test_app_*.py`) → merges will be trivially clean (no shared __init__/CLAUDE.md unlike the domain waves). Clusters:
1. mig-dlgen: download, generate(+plans/retry), language (flagship: test_download_multi_artifact wholesale MOVE).
2. mig-source: all 7 source_* modules (heaviest; TestSourceCleanClassify free MOVE).
3. mig-misc: skill, doctor (ZERO coverage today), research, events.
4. mig-crud: notebooks, notes, labels, sharing, chat (ZERO coverage today).
5. mig-auth: session, auth_check, profile (all net-new — auth-wave modules; mostly net-new app tests).
Guardrails: tests-only (no src/ logic edits); NEVER reduce coverage (MOVE relocates the assertion); do NOT touch
resolve (reserved for M2); KEEP genuinely-CLI tests; full suite collected count must stay ≥ 8910 baseline.
M2 (serialized, AFTER M1 merges): resolve consolidation — collapse `cli/resolve.py` parallel impl into
`_app/resolve.py`, delete the ~40 duplicate test_resolve.py tests, preserve the 29 ClickException patch seams.

## M1 partial-merge: mig-misc/auth/crud merged clean (disjoint files; +172 app tests → 317 total, all green). dlgen+source pending.
Per the disjoint-work principle: merged the 3 completed clusters immediately rather than waiting for the other 2.

## DEDUP STAGE (user decision 2026-06-08: "targeted subsumption audit") — runs AFTER all M1 merges.
The migration's SPLIT verdict was "add `_app` test AND slim the CLI test to a thin shell" — but several clusters
(crud/auth) did pure-ADD and LEFT the full CLI tests intact, so some neutral logic is now asserted at BOTH layers.
Most of the growth is INTENDED (23 modules had zero direct coverage; doctor/chat had none) — NOT waste. The genuine
redundancy gets a TWO-part trim:
1. **Resolve consolidation (M2):** collapse `cli/resolve.py` parallel impl into `_app/resolve.py`; delete the ~40
   true-duplicate test_resolve.py tests; preserve the 29 ClickException patch seams.
2. **Subsumption audit:** find CLI tests now FULLY subsumed by a new `_app` test → delete; slim partially-redundant
   ones to CLI-only assertions (envelope/exit/render). KEEP cassette/integration tests (they prove the real
   end-to-end path) + all net-new coverage for previously-untested modules.
GATE for the trim (mandate flips from "never reduce coverage" to "reduce only REDUNDANT coverage"): `pytest --cov`
must show NO `src/` coverage regression vs pre-trim — a dropped line/branch means it wasn't actually redundant.

## M1 COMPLETE — all 5 clusters merged. App tests 145 → 623 (+478 direct). Full suite 9238 passed / 0 failed.

## DEDUP STAGE — RESOLVED (user 2026-06-08: "Keep suite; skip cutting"). Findings that drove it:
- **Resolve consolidation DROPPED.** The map's "~40 true duplicates" did NOT survive code inspection: `cli/resolve.py`
  is a legitimate rich ADAPTER (raises ClickException not ValidationError; adds entity_name/list_command hints to
  messages; has `allow_full_id_passthrough`, `error_factory`, console `emit_status`) over the pure `_app/resolve.py`
  CORE. `test_resolve.py` (59 tests) overwhelmingly tests THAT cli surface (82 refs) + `require_notebook`'s
  env/context/SystemExit ladder — NOT the matching rules. It's an adapter/core split, not duplication. Consolidating
  = behavior-touching refactor (case-differing exact-match emit, download no-passthrough path) for zero user benefit.
- **No cutting.** Measured `_app` coverage: CLI tests alone 89% vs app tests 95%. The ~89% overlap is LAYERED
  (unit + integration), not same-layer redundant — the `--cov` gate would block almost any deletion. The new app
  tests add +6% unique coverage, run 5× faster (2s vs 10.6s), localize failures, and are the MCP/HTTP-reuse basis.
  The growth is intended new direct coverage (doctor/chat had zero) + healthy defense-in-depth. Suite ~50s; runtime
  is a non-issue. Decision: KEEP.

## FINAL INTEGRATION (in progress): error_handler→`_app.classify` routing; the relocation ADR.
## DEFERRED (per user): feat/mcp-server rebase + delete mcp/_serialize|_ids|_errors; de-monkeypatch via ctx.obj.
