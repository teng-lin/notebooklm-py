# ADR-0013: Composable Session Capabilities and Feature-Local Runtimes

> **Historical note (2026-06-11).** This ADR is **Accepted** and its core
> decision — composable, per-capability Protocols instead of one fat `Session`
> contract — remains in force. However, several *names* it cites have changed
> since it was written, and some of the feature-local composites it proposed
> were later retired. In particular: the contracts module
> `_session_contracts.py` was replaced by `_runtime/contracts.py`; the
> concrete `Session` facade was **deleted**; and the feature-local composite
> Protocols `ChatRuntime`, `ArtifactsRuntime`, and `UploadRuntime` (Decision
> §3) were **retired** in favour of feature constructors taking their narrow
> collaborators by keyword-only argument (see
> [ADR-0014](0014-feature-local-runtime-adapters.md) and the
> `_runtime/contracts.py` module docstring). Read the specific module names,
> Protocol names, and `file.py:NNN` line numbers below as historical; the
> current source tree and [`docs/architecture.md`](../architecture.md) are
> authoritative. The live shared capability Protocols today are `Kernel`,
> `RpcCaller`, and `LoopGuard` in `_runtime/contracts.py`; `AuthMetadata` is
> local to `_source/upload.py`, `OperationScopeProvider` is local to
> `_artifact/polling.py`, and `AsyncWorkRuntime` was deleted.

## Status

Accepted.

This ADR ratifies the capability-composition model originally proposed
in `docs/refactor-history.md` (revision 5, dated 2026-05-20). It supersedes
[ADR-0010](0010-session-kernel-split.md) (Session/Kernel split), which
was re-statused to `Superseded by ADR-0013 (#866)` when this ADR landed.

The 11-step migration originally drafted alongside this ADR landed in
full across Phases 1–7 of the capability refactor arc; the broad
`Session` protocol was deleted in Phase 7, so this ADR is now a plain
`Accepted` record with no outstanding sunset clause.

## Context

ADR-0010 (Tier 13 PR 13.1) pinned a deliberately narrow feature-facing
`Session: Protocol` with **exactly five members** — `rpc_call`,
`transport_post`, `next_reqid`, `assert_bound_loop`, and `operation_scope`
— alongside a three-member `Kernel: Protocol` and a one-member
`DrainHookRegistration: Protocol`. The intent was that feature APIs would
converge on one semantic orchestration contract, while transport stayed
isolated and Artifacts received a dedicated drain-hook seam.

That narrow contract did not hold. At the time this ADR was written, the
broad `Session` Protocol in `src/notebooklm/_session_contracts.py` had
grown to **eight members**:

1. `auth` (an `AuthMetadata` property)
2. `kernel` (a `Kernel` property)
3. `rpc_call(...)`
4. `transport_post(...)`
5. `next_reqid(...)`
6. `assert_bound_loop()`
7. `operation_scope(...)`
8. `register_drain_hook(...)`

The five-member intent of ADR-0010 was gone. `auth` and `kernel` had been
promoted as members for upload-flow convenience; `register_drain_hook`
had been added to the general contract despite a standalone
`DrainHookRegistration` Protocol covering the same shape — leaving two
redundant protocols carrying the same single-member surface.
`transport_post` and `next_reqid` were used by exactly one feature
(chat), but every feature that typed against `Session` was coupled to
them. (Post-this-ADR, Phase 7 deleted the broad `Session` Protocol
entirely; current `_runtime/contracts.py` exposes only `Kernel`,
`RpcCaller`, and `LoopGuard`.)

Re-reading the codebase against ADR-0010's original constraint, the audit
identified two categories of capability:

- **SHARED**: a capability used by ≥2 features today, justifying
  promotion to a module-level Protocol in `_runtime/contracts.py`.
  Examples: logical RPC dispatch (`rpc_call`) is used by every feature
  API; loop-affinity assertion is used by chat plus artifact polling.
  The concrete `TransportDrainTracker.operation_scope(...)` helper is
  used by sources upload plus artifact polling, but the named
  `OperationScopeProvider` Protocol is no longer a shared contract; it
  lives beside artifact polling, its only Protocol consumer.
- **FEATURE-LOCAL**: a capability used by exactly one feature, with no
  current second consumer. Examples: `transport_post(...)` + chat's
  manual `next_reqid(...)` bookkeeping (only chat needs them);
  drain-hook registration (only artifact polling registers a close-time
  hook today).

Mixing the two categories into one fat `Session` Protocol forces every
feature to declare a structural dependency on capabilities it never
calls. It also encourages "promote it just in case" drift: the
`auth`/`kernel`/`register_drain_hook` additions happened precisely
because there was no convention saying *don't widen the shared contract
unless a second consumer exists*.

Two adjacent service boundaries are entangled in the same shape problem
and must move with this ADR:

- The `_mind_map.py` module currently bundles a generic note-row CRUD
  service together with a mind-map-specific adapter. The two have
  different consumers (the mind-map adapter is consumed by Artifacts
  download paths; the generic note service is consumed by the note
  generation path), and they need different lifetimes during the
  migration.
- Saving a chat `AskResult` as a note currently lives on `NotesAPI`,
  but it depends on chat's response shape and chat's conversation cache.
  This is the wrong direction of dependency — the owner of the data
  should own the persistence call.

The pre-existing scaffolding from earlier remediation work makes a
careful migration tractable: `NotebookSourceLister`
(`_notebook_metadata.py:19`) and `NotebookSourceIdProvider`
(`_notebook_metadata.py:26`) already let `NotebooksAPI` accept a
collaborator-shaped dependency without round-tripping through `Session`.
`_mind_map.py:55` is the existing service boundary the note/mind-map
split rides on.

## Decision

The library adopts a **composable capability model** with feature-local
runtimes. Concretely:

1. **Promote shared capability Protocols** to the shared contracts module
   only when they have at least two production consumers. At decision
   time that meant `RpcCaller`, `LoopGuard`, `OperationScopeProvider`,
   and `AsyncWorkRuntime` in `_session_contracts.py`. Current code has
   demoted/deleted the single-consumer pieces: `_runtime/contracts.py`
   exports only `Kernel`, `RpcCaller`, and `LoopGuard`.

   The symmetric demotion / sibling-fold rule lives in
   [ADR-0012 §Demotion / consolidation rule](0012-implementation-surface-convention.md#demotion-consolidation-rule):
   when a previously-shared capability drops back to a single consumer,
   or when two underscore-prefixed seam siblings become small enough
   and mutually-isolated, the ADR-0012 rule authorizes the inverse
   motion. The two rules read as a paired policy — ADR-0013 §1 governs
   promotion *up* into a shared Protocol; ADR-0012 §Demotion governs
   collapse *back down* into a feature-local Protocol or a folded
   seam file. Neither motion needs a fresh ADR; only changes to the
   criteria themselves do.

2. **Do not put account metadata on a broad runtime facade.** At decision
   time `AuthMetadata` and `Kernel` stayed as standalone Protocols in the
   shared contracts module. Current code keeps `Kernel` in
   `_runtime/contracts.py` as the typed transport surface and moves the
   single-consumer `AuthMetadata` Protocol local to `_source/upload.py`.

3. **Define feature-local runtime Protocols in their owning module** when
   a named composite earns its keep. At decision time these were:
   - `ChatRuntime` in `_chat.py` (composes `RpcCaller + LoopGuard`
     plus chat-only `transport_post(...)` and `next_reqid(...)`).
   - `ArtifactsRuntime` and `DrainHookRegistration` in `_artifacts.py`
     (composes `RpcCaller + AsyncWorkRuntime + DrainHookRegistration`).
   - `UploadRuntime` in `_source/upload.py` (historically `_source_upload.py`)
     (composes `RpcCaller +
     OperationScopeProvider + LoopGuard` plus `kernel` + `auth`
     constructor args).

   Current code deleted those composites/adapters after ADR-0014 showed
   direct collaborator injection was clearer for their single consumers.

4. **Each feature constructor names its dependency by capability**, not
   by the broad `Session`:
   - Pure-RPC features (`NotebooksAPI`, `ResearchAPI`, `SettingsAPI`,
     `SharingAPI`) take `rpc: RpcCaller`.
   - Multi-capability features (`ChatAPI`, `ArtifactsAPI`,
     `SourceUploadPipeline`) take direct keyword-only collaborators
     (`rpc`, `transport`, `reqid`, `loop_guard`, `drain`, `lifecycle`,
     `kernel`, `auth`) rather than a broad or composite runtime object.

5. **Split `_mind_map.py`** into a private `NoteService` (new
   `_note_service.py`) and a `NoteBackedMindMapService` (mind-map
   adapter that stays in `_mind_map.py`). The pre-existing scaffolding
   the refactor relies on — `NotebookSourceLister`
   (`_notebook_metadata.py:19`), `NotebookSourceIdProvider`
   (`_notebook_metadata.py:26`), and the `_mind_map.py:55` service
   boundary — is reused. Module-level `_mind_map` wrappers are
   removed only after both collaborators are wired into `ArtifactsAPI`.

6. **Move saved-chat-from-`AskResult` ownership from `NotesAPI` to
   `ChatAPI`**: `ChatAPI.save_answer_as_note(notebook_id, ask_result, *,
   title: str | None = None) -> Note`. Define a `SaveChatAnswerCallback`
   type alias used by the deprecated `NotesAPI.create_from_chat`
   forwarder during the migration window. That forwarder was removed
   in v0.7.0; `ChatAPI.save_answer_as_note(...)` is the surviving API.

7. **No mixins for dependency expression.** Required capabilities are
   declared with `Protocol`s; extracted behavior is held by
   collaborators/services. This restates `docs/refactor-history.md` §Design
   Rule 6 and is binding for this refactor and future feature work.

8. **Underscore-prefix module privacy from
   [ADR-0012](0012-implementation-surface-convention.md) still applies.**
   This ADR does not change the public surface: `NotebookLMClient` and
   every `client.<feature>.<method>` reachable at v0.4.1 keeps its
   signature, defaults, and return type. Only private constructors and
   helper-module internals move.

## Consequences

**Wanted**

- Capability subsets are mypy-verified per feature. Adding a new
  feature picks the narrowest needed slice
  (`RpcCaller`, `LoopGuard`, `Kernel`, or a local Protocol) instead
  of opting into the entire `Session` surface.
- Feature-local capabilities evolve without widening any shared union.
  If chat later needs another streaming primitive, the change is local
  to `_chat/` and the chat helpers, not to every feature that types
  against `RpcCaller`.
- `_runtime/contracts.py` shrank after the migration arc deleted the
  broad `Session` Protocol and later demoted single-consumer shapes. It
  now contains only `Kernel`, `RpcCaller`, and `LoopGuard`.
- The `auth/kernel/register_drain_hook` drift pattern is structurally
  prevented: `_runtime/contracts.py` accepts new shared Protocols only when a
  second consumer exists in the codebase at promotion time.
- The note/mind-map service split lets Artifacts and Notes evolve their
  internal storage paths independently. The mind-map adapter can be
  rewritten without touching the generic note service.

**Unwanted**

- ≥36 direct feature-API test constructors must be updated alongside
  each migration step. Tests that instantiate `ChatAPI(session)`,
  `NotesAPI(session)`, `ArtifactsAPI(session)`, etc., must switch to
  the new keyword-only collaborator arguments. The migration steps in
  `docs/refactor-history.md` pair each feature retyping with its same-commit
  test fixture update so the build stays green.
- The `_core.py` compatibility shim was removed in Phase 4 ([#889](https://github.com/teng-lin/notebooklm-py/pull/889)); see `tests/_guardrails/test_public_surface_manifest.py` (search for `Tier-10 PR-A re-export identity pins for ``notebooklm._core`` were deleted`) for the removal pin.
- Two `RpcCaller` Protocols coexist briefly: the shared *object*
  protocol in `_runtime/contracts.py` (symbol `RpcCaller`, used by every
  feature API) and a pre-existing local *callable* protocol in
  `_source/upload.py` (historically `_source_upload.py`, symbol `RpcCallback`, used as the
  `register_file_source(rpc_call=...)` callback). They are structurally
  distinct (one is an object with an `rpc_call` method; the other is a
  callable). To avoid the name collision, the local callable protocol is
  **renamed** to `RpcCallback` in the same commit that introduces the
  shared `RpcCaller`. The local protocol is not deleted because the
  callback shape is a real seam the upload pipeline depends on.
- This ADR exceeds the 250-line soft cap by user direction; the hard
  cap remains 500 lines. The expanded length is load-bearing: the
  decision shipping in this PR is the simultaneous adoption of three
  related changes (capability composition, note/mind-map split, and
  saved-chat ownership move) and they would not be coherent if split
  across multiple ADRs.

The incremental migration that realized this decision shipped across
Phases 1–7 of the capability refactor arc and is now complete; this ADR
records the architectural intent rather than the step sequence. See
`docs/refactor-history.md` for the as-shipped module map and the
narrative of how the cutover landed.

### Composed Runtime Consequences

- **C-X. `cookie_saver` / `cookie_rotator` late-binding seams** — introduced by PR [#879](https://github.com/teng-lin/notebooklm-py/pull/879) (`76b301d`). The cookie persistence hooks are now resolved through `_runtime/lifecycle.py` / `_runtime/init.py`, decoupling persistence logic from the core HTTP client transport and ensuring clean integration with the cookie keepalive loop.
- **C-Y. Inline `__Secure-1PSIDTS` cold-start recovery** — introduced by PR [#872](https://github.com/teng-lin/notebooklm-py/pull/872) / issue #865 (`6d8b5f4`). Production utilizes `_recover_psidts_inline` in `_auth/psidts_recovery.py` to run a preflight healing check. If preconditions are met (e.g. not bypassing credentials in environment-driven auth, and utilizing a flock-based cross-process lock), the system proactively mints a valid `__Secure-1PSIDTS` cookie before initializing the session facade to prevent cold-start failures.
- **C-Z. AST-based delegate-surface regression guard** *(historical)* — introduced by PR [#885](https://github.com/teng-lin/notebooklm-py/pull/885) (`f48d4b9`). It used AST analysis to pin simple delegate forwards on the now-deleted `Session` facade. Current guardrails instead prevent the deleted session/runtime-boundary surfaces from returning.
- **C-AA. Drain-hook registration is owned by the transport drain tracker** — The standalone `DrainHookRegistration` Protocol from the Session/Kernel split was retired. *Current state (2026-06):* `DrainHookRegistration` is not a Protocol local to an `ArtifactsRuntime` (that composite no longer exists). The registration surface collapsed onto `TransportDrainTracker.register_drain_hook(...)` in `src/notebooklm/_transport_drain.py`. Feature code that owns long-running async work — artifact polling in `_artifact/polling.py` — registers its close-time hook there, and `ClientLifecycle.close` fires the registered hooks. See ADR-0014.
- **C-AB. Narrow executor host Protocols after bridge retirement** *(historical)* — The session-shrink arc narrowed the former `RpcOwner` host Protocol after the legacy `Session` private-attribute shims were retired, dropping `_timeout`, `_refresh_callback`, `_refresh_retry_delay`, and `_http_client`. *Current state (2026-06):* `RpcOwner` was deleted entirely (session-decoupling Wave 4, #1068); `RpcExecutor` (`_rpc_executor.py`) now takes its `Kernel`, `RuntimeTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters and keeps only a local `DecodeResponse` Protocol.
- **C-AC. Retire the legacy transport Adapter** — The middleware chain terminal now calls `Kernel.post` through `MiddlewareChainHost._authed_post_chain_terminal -> RuntimeTransport.terminal`. Request construction lives in `_request_types.py`, transport exceptions and `Retry-After` parsing live in `_transport_errors.py`, and size-capped streaming lives in `_streaming_post.py`. This removed the last legacy host Protocol and the shallow Adapter seam.

## Alternatives considered

1. **Keep the broad `Session` contract and let it continue to grow.**
   Rejected. At ADR-write time the drift was already visible in
   `_session_contracts.py`'s broad `Session` Protocol: ADR-0010 specified
   five members, and the contract had grown to eight. Without an
   explicit promotion criterion (shared by ≥2 features), every future
   single-consumer capability is a candidate for promotion, and the
   contract is one PR away from nine members. The "narrow Session"
   intent of ADR-0010 is not recoverable by exhortation; it requires a
   structural rule.

2. **Per-sub-client narrow Protocols only, no feature-local runtimes**
   (the post-D2 replacement from the ADR-0002 lineage). Rejected.
   `ChatRuntime`'s `transport_post + next_reqid` slice cannot be
   modelled this way without either (a) widening a shared Protocol to
   include capabilities only chat uses, or (b) duplicating the
   `RpcCaller + LoopGuard` Protocol definitions across multiple
   feature modules. Composition via Protocol inheritance
   (`ChatRuntime(RpcCaller, LoopGuard, Protocol)`) gives the same
   narrow-typing benefit without either downside.

3. **Promote `transport_post` and `next_reqid` globally to shared
   Protocols.** Rejected. There is no second consumer today. Promoting
   on speculation is exactly the drift pattern this ADR fights:
   `auth`/`kernel`/`register_drain_hook` were promoted to the broad
   `Session` for "convenience" without a second-consumer trigger, and
   the result was the eight-member contract enumerated in the Context
   section above. The promotion criterion in Decision §1
   (shared by ≥2 features) exists precisely to block this path. If a
   future feature genuinely needs chat's transport slice, the
   capability is promoted at that point with the second consumer as
   evidence.

4. **Split into multiple ADRs (capability composition + note/mind-map
   split + saved-chat ownership move).** Rejected per user direction.
   The three changes are not independent: the note/mind-map split is a
   prerequisite for `ArtifactsAPI` taking `mind_maps` and
   `note_service` collaborators (Decision §5), and the saved-chat
   ownership move depends on `ChatAPI` already taking a `ChatRuntime`
   (Decision §6). Splitting the ADR would either force a fragile
   inter-ADR ordering or hide the unified design intent behind three
   smaller records that each look like "just a refactor". One ADR
   keeps the decision narrative intact and explicit about the three
   changes shipping together.
