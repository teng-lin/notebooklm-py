# Architecture Remediation Completion Status

Status: T14.1 completion note.

Generated from the parent remediation plan, the T14.0 final promotion audit,
merged GitHub PR metadata, and the Phase 11 T13/T14 plan. The original
architecture audit and remediation plan remain immutable evidence.

## T14.0 Readiness Snapshot

- Base: `origin/main` at `77802dcc4461a6c45296a2b583f7146fe4d542a5`.
- T14.0 status: complete. The audit evidence is embedded in this completion
  note.
- #764 is merged into `main`.
- T13.5 final verification passed on merged `main`.
- This note is docs-only. T14.2 owns final static guardrail edits.

T13.5 merged-main verification from the T14.0 audit:

- Public facade and `_types` smoke script passed.
- Targeted type/public-shim/CLI JSON suite: 549 passed.
- Targeted integration/API/CLI suite: 836 passed, 3 warnings.
- Ruff check passed.
- Mypy passed, 110 files checked.

Final boundaries observed during readiness:

- Auth facade over `_auth/*` modules.
- Core collaborator modules for cache, polling, cookie persistence, transport,
  and RPC execution.
- Feature service modules for artifacts, sources, notebook metadata, sharing,
  notes, mind-map, and CLI services.
- CLI runtime, auth-runtime, context, rendering, resolve, input, completion, and
  service modules.
- RPC override policy outside `notebooklm.rpc.types`.
- `notebooklm.types` facade over `_types/*` modules with public compatibility
  behavior preserved.

Deferred items that T14 should not fix:

- Full `RuntimeConfig` / `ConfigurationStore`.
- Shared cache primitive.
- Optional non-filesystem `AuthStorage` backends.
- Retained compatibility alias surfaces and deprecated shims:
  `notebooklm.types.ArtifactTypeCode` outside `__all__`, deprecated top-level
  `notebooklm.StudioContentType`, `notebooklm.rpc.types.StudioContentType`,
  public delegators, CLI helper facade re-exports/patch seams, and legacy
  `NotebooksAPI.share()`.
- Artifact service-injection follow-up tracked outside T14.

## Finding Matrix

| Finding | Final Status | Phase/Task | PR | Merge SHA | Guard Status | Verification Evidence | Deferred Owner |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1. Public/private contract inconsistent | Implemented | T1, T2, T13.0, T13.4, T14.0 | #646, #647, #752, #764 | `10664d86`, `636b8ecf`, `320a950e`, `77802dcc` | Guarded by public shim, CLI boundary, init-order, and type-boundary tests; T14.2 may tighten final static checks | Phase 1 audit clean; T13.0 froze the type contract; T14.0 audit records #764 merged and T13.5 passed on `main` | None |
| 2. `auth.py` subsystem | Implemented | T3, T4, T5 | #649, #650, #652, #653, #661 | `f24eb69a`, `d9529b6e`, `3f1b2db0`, `eeea83ce`, `518a0429` | Guarded by auth facade, public shim, and core/auth boundary tests | Phase 2 audit clean; Phase 4 audit clean; targeted auth and integration checks passed | Optional `AuthStorage` backends deferred below |
| 3. `ClientCore` too broad | Implemented | T4, T5, T6 | #654, #655, #656, #657, #658, #661, #662, #663 | `03395a86`, `e8e58767`, `1b75f2e5`, `935be22f`, `d89a6005`, `518a0429`, `4bf3cd3f`, `1cb7cee8` | Guarded by core collaborator, capability, init-order, and private-state tests | Phase 3 audit clean with full pytest, ruff, format check, and mypy passing | Shared cache primitive deferred below |
| 4. `refresh_auth()` crosses boundaries | Implemented | T5 | #661 | `518a0429` | Guarded by auth session and core/auth tests | Phase 4 audit clean; targeted refresh/auth tests, ruff, and mypy passed | None |
| 5. Feature APIs receive full core | Implemented; residual feature injections are explicit follow-ups | T6, T8, T9, T10 | #662, #663, #666, #676, #687, #690 | `4bf3cd3f`, `1cb7cee8`, `f1146e0d`, `c18d15bd`, `2bfa2625`, `42d71f4c` | Guarded by capability tests, feature service tests, and init-order/private-state checks | Phase 5 audit clean; artifact, source, and notebook phase audits clean | Artifact service-injection follow-up remains outside T14 |
| 6. `ArtifactsAPI` mixes responsibilities | Implemented with known follow-up outside T14 | T8 | #666, #668, #670, #672, #673, #674 | `f1146e0d`, `8057cf5f`, `f0ee3dae`, `042c4366`, `74b75149`, `4f17315d` | Guarded by artifact service tests and import-boundary checks; T14.2 may add only landed-boundary guards | Phase 7 artifact audit passed on main with full pytest, ruff, format check, and mypy passing | Artifact service-injection follow-up tracked outside T14 |
| 7. `SourcesAPI` mixes responsibilities | Implemented | T9 | #676, #677, #678, #679, #683, #684, #685 | `c18d15bd`, `fc67044e`, `c39eefa0`, `cd7be3e7`, `fcbd0f2c`, `6cac1639`, `a6821a10` | Guarded by source service tests and static source-service import guard | Phase 8 source audit passed on main with targeted unit, integration, full pytest, ruff, format check, and mypy passing | None |
| 8. CLI modules mix command/app/presenter | Implemented | T11, T12, T14.0 | #694, #695, #696, #697, #698, #701, #703, #705, #699, #702, #704, #711, #712, #713, #716, #720, #724, #745 | `81c168d7`, `aa2e8f04`, `d7792bd7`, `8cc23e34`, `04ee1115`, `0ef8808a`, `d2389d42`, `a59e69da`, `43c9476d`, `048b2815`, `ea756ed3`, `2be3c832`, `d10c5ab4`, `8521b30e`, `8de39e53`, `cb18b8fe`, `ff6252cd`, `96267610` | Guarded by CLI contract, helper facade, completion boundary, and CLI import tests; T14.2 owns final hardening | T14.0 audit records T11/T12 merge evidence and T13.5 merged-main CLI verification passing | Retained CLI helper compatibility alias row below |
| 9. `cli/helpers.py` boundary sink | Implemented as compatibility facade | T12 | #699, #702, #704, #713, #711, #716, #720, #712, #724, #745 | `43c9476d`, `048b2815`, `ea756ed3`, `8521b30e`, `2be3c832`, `8de39e53`, `cb18b8fe`, `d10c5ab4`, `ff6252cd`, `96267610` | Guarded by `test_helpers_remains_compatibility_facade` and CLI boundary checks | Phase 10 helper seam map recorded compatibility requirements; merged T12 slices preserve helper patch seams | Retained CLI helper compatibility alias row below |
| 10. Download CLI bypasses runtime | Implemented | T11 | #697, #701 | `8cc23e34`, `0ef8808a` | Guarded by download runtime tests and CLI boundary tests | Download runtime normalization PRs merged; later main integration retained the runtime route | None |
| 11. Streaming chat parallel protocol | Implemented | T7 | #665 | `0bd6bbd0` | Guarded by streaming chat protocol tests | Phase 6 audit clean; targeted streaming/RPC override checks passed | None |
| 12. RPC types own env policy | Implemented | T7 | #664 | `ec9859fc` | Guarded by RPC override tests and import-order tests | Phase 6 audit clean; `NOTEBOOKLM_RPC_OVERRIDES` policy moved out of protocol enum definitions | Full `RuntimeConfig` row below |
| 13. `NotebooksAPI` cross-namespace behavior | Implemented | T10 | #687, #688, #689, #690, #692, #693 | `2bfa2625`, `6e203d3b`, `da8f974d`, `42d71f4c`, `1f617ad6`, `3fd8f9ac` | Guarded by notebook, sharing, mind-map, init-order, and private service guardrail tests | Phase 9 final audit passed on main with full pytest, ruff, format check, mypy, pre-commit, and review-thread audit passing | None |
| 14. CLI option decorators do live runtime work | Implemented | T11, T12 | #695, #696, #698, #703, #705, #745 | `aa2e8f04`, `d7792bd7`, `04ee1115`, `d2389d42`, `a59e69da`, `96267610` | Guarded by completion provider and CLI boundary tests | Runtime/completion characterization and guardrail PRs merged; final helper facade guardrails merged | None |
| 15. `_mind_map` primitive-layer mismatch | Implemented with compatibility seams retained | T8, T10 | #666, #690 | `f1146e0d`, `42d71f4c` | Guarded by mind-map service tests and notebook service import guards | T10c extracted mind-map service contract; Phase 9 final audit passed | Compatibility alias row below |
| 16. `types.py` monolith | Implemented; T13.5 verified on merged `main` | T13.0, T13.1, T13.2, T13.3, T13.4, T13.5 | #752, #753, #757, #763, #764 | `320a950e`, `11fdad0e`, `54747c39`, `49847908`, `77802dcc` | Guarded by type-boundary tests from #764; T14.2 owns any final static hardening | T14.0 audit records public facade smoke passing, 549 targeted type/public-shim tests passing, 836 targeted integration/API/CLI tests passing, ruff passing, and mypy passing | Retained type facade compatibility alias row below |
| Runtime config unification | Deferred | Parent deferred item; T7 only extracted RPC override policy | #664 | `ec9859fc` | Not applicable to T14.1; do not create failing guards for unlanded full config store | Parent plan explicitly deferred full `RuntimeConfig` / `ConfigurationStore`; T7 moved only RPC override policy | Post-T14 maintainer follow-up |
| Shared cache primitive | Deferred | Parent deferred item; T4 extracted conversation cache first | #654 | `03395a86` | Not applicable to T14.1; guard only existing cache collaborators | Parent plan deferred shared primitive until duplication remains meaningful after cache and override extraction | Post-T14 maintainer follow-up |
| AuthStorage backend implementations | Deferred | Parent deferred item; T3 implemented filesystem/auth storage split only | #653 | `eeea83ce` | Not applicable to T14.1; existing auth storage surface remains guarded | Parent plan deferred Redis/cloud-secrets style backends; T3d merged storage/account helper split | Auth maintainers after architecture remediation |
| Retained compatibility alias surfaces | Retained intentionally; cleanup deferred to deprecation policy | T1-T13 compatibility contracts | #647, #699, #745, #752, #764 | `636b8ecf`, `43c9476d`, `96267610`, `320a950e`, `77802dcc` | Guarded by public shim, helper facade, CLI boundary, RPC enum identity, and type-boundary tests | T14.0 audit records retained aliases: `notebooklm.types.ArtifactTypeCode` outside `__all__`, deprecated top-level `notebooklm.StudioContentType`, `notebooklm.rpc.types.StudioContentType`, public delegators, CLI helper facade re-exports/patch seams, and legacy `NotebooksAPI.share()` | Compatibility/deprecation follow-up owners |

## Residual Risk Notes

- This file records T14.1 completion status. T14.3/T14.4 still own the final
  verification matrix and residual release-readiness decision.
- T14.0 is complete and this T14.1 branch is based on `origin/main`, not the old
  #764 stack branch.
- T14.2 may add or tighten static guardrails only for boundaries that already
  exist. It should not move implementation code or rewrite compatibility
  surfaces.
