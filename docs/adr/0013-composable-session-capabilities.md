# ADR-013: Composable Session Capabilities and Feature-Local Runtimes

## Status

Accepted.

This ADR ratifies the capability-composition model originally proposed
in `docs/refactor-history.md` (revision 5, dated 2026-05-20). It supersedes
[ADR-010](0010-session-kernel-split.md) (Session/Kernel split), which
was re-statused to `Superseded by ADR-013 (#866)` when this ADR landed.

The 11-step migration described in `docs/refactor-history.md` §Migration Plan
landed in full across Phases 1–7 of the capability refactor arc; the
broad `Session` protocol was deleted in Phase 7 (refactor-history.md step 10),
so this ADR is now a plain `Accepted` record with no outstanding
sunset clause.

## Context

ADR-010 (Tier 13 PR 13.1) pinned a deliberately narrow feature-facing
`Session: Protocol` with **exactly five members** — `rpc_call`,
`transport_post`, `next_reqid`, `assert_bound_loop`, and `operation_scope`
— alongside a three-member `Kernel: Protocol` and a one-member
`DrainHookRegistration: Protocol`. The intent was that feature APIs would
converge on one semantic orchestration contract, while transport stayed
isolated and Artifacts received a dedicated drain-hook seam.

That narrow contract did not hold. Today, at
`src/notebooklm/_session_contracts.py:50`, the broad `Session` protocol
exposes **eight members**:

1. `auth` (`AuthMetadata` property; `_session_contracts.py:54`)
2. `kernel` (`Kernel` property; `_session_contracts.py:57`)
3. `rpc_call(...)` (`_session_contracts.py:59`)
4. `transport_post(...)` (`_session_contracts.py:71`)
5. `next_reqid(...)` (`_session_contracts.py:79`)
6. `assert_bound_loop()` (`_session_contracts.py:81`)
7. `operation_scope(...)` (`_session_contracts.py:83`)
8. `register_drain_hook(...)` (`_session_contracts.py:85`)

The five-member intent of ADR-010 is gone. `auth` and `kernel` were
promoted as members for upload-flow convenience; `register_drain_hook`
was added to the general contract despite the standalone
`DrainHookRegistration` Protocol at `_session_contracts.py:92` covering
the same shape — leaving two redundant protocols carrying the same
single-member surface. `transport_post` and `next_reqid` are used by
exactly one feature (chat), but every feature that types against
`Session` is now coupled to them.

Re-reading the codebase against ADR-010's original constraint, the audit
identified two categories of capability:

- **SHARED**: a capability used by ≥2 features today, justifying
  promotion to a module-level Protocol in `_session_contracts.py`.
  Examples: logical RPC dispatch (`rpc_call`) is used by every feature
  API; loop-affinity assertion is used by chat plus artifact polling;
  `operation_scope(...)` is used by sources upload plus artifact
  polling.
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

1. **Promote four shared capability Protocols** to
   `_session_contracts.py`: `RpcCaller`, `LoopGuard`,
   `OperationScopeProvider`, and `AsyncWorkRuntime` (which composes
   `LoopGuard + OperationScopeProvider`). The promotion criterion is
   **shared by ≥2 features**. No capability is promoted on speculation.

2. **Retain `AuthMetadata` (`_session_contracts.py:24`) and `Kernel`
   (`_session_contracts.py:34`)** as standalone Protocols in
   `_session_contracts.py` — they are **NOT** members of any
   feature-facing Session Protocol. Only `SourceUploadPipeline`
   consumes them, but the upload pipeline still depends on Session-owned
   objects (the authenticated account snapshot and the transport
   kernel), so the Protocols stay in the shared contracts module rather
   than moving into `_source_upload.py`.

3. **Define feature-local runtime Protocols in their owning module**:
   - `ChatRuntime` in `_chat.py` (composes `RpcCaller + LoopGuard`
     plus chat-only `transport_post(...)` and `next_reqid(...)`).
   - `ArtifactsRuntime` and `DrainHookRegistration` in `_artifacts.py`
     (composes `RpcCaller + AsyncWorkRuntime + DrainHookRegistration`).
   - `UploadRuntime` in `_source_upload.py` (composes `RpcCaller +
     OperationScopeProvider` plus `kernel` + `auth` constructor args).

4. **Each feature constructor names its dependency by capability**, not
   by the broad `Session`:
   - Pure-RPC features (`NotebooksAPI`, `ResearchAPI`, `SettingsAPI`,
     `SharingAPI`) take `rpc: RpcCaller`.
   - Composite features (`ChatAPI`, `ArtifactsAPI`, `SourcesAPI`) take
     `runtime: <FeatureRuntime>`.

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
   forwarder, which keeps emitting a `DeprecationWarning` and forwards
   to the new chat-owned method for one minor-version cycle.

7. **No mixins for dependency expression.** Required capabilities are
   declared with `Protocol`s; extracted behavior is held by
   collaborators/services. This restates `docs/refactor-history.md` §Design
   Rule 6 and is binding for this refactor and future feature work.

8. **Underscore-prefix module privacy from
   [ADR-012](0012-implementation-surface-convention.md) still applies.**
   This ADR does not change the public surface: `NotebookLMClient` and
   every `client.<feature>.<method>` reachable at v0.4.1 keeps its
   signature, defaults, and return type. Only private constructors and
   helper-module internals move.

## Consequences

**Wanted**

- Capability subsets are mypy-verified per feature. Adding a new
  feature picks the narrowest needed slice
  (`RpcCaller`, `AsyncWorkRuntime`, or a feature-local runtime) instead
  of opting into the entire `Session` surface.
- Feature-local runtimes evolve without widening any shared union. If
  `ChatRuntime` later needs a streaming primitive, the change is local
  to `_chat.py` and the chat helpers, not to every feature that types
  against `Session`.
- `_session_contracts.py` shrinks as Phase 7 of the migration arc
  deletes the broad `Session` Protocol. Post-cutover the module
  contains only the four shared capability Protocols plus
  `AuthMetadata` and `Kernel`.
- The `auth/kernel/register_drain_hook` drift pattern is structurally
  prevented: `_session_contracts.py` accepts new Protocols only when a
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
- The `_core.py` compatibility shim was removed in Phase 4 ([#889](https://github.com/teng-lin/notebooklm-py/pull/889)); see `tests/unit/test_public_shims.py:1166` for the removal pin.
- Two `RpcCaller` Protocols coexist briefly: the shared *object*
  protocol in `_session_contracts.py` (used by every feature API) and a
  pre-existing local *callable* protocol in `_source_upload.py:43-56`
  (used as the `register_file_source(rpc_call=...)` callback at
  `_source_upload.py:345`). They are structurally distinct (one is an
  object with an `rpc_call` method; the other is a callable). To avoid
  the name collision, the local callable protocol is **renamed** to
  `RpcCallback` in the same commit that introduces the shared
  `RpcCaller`. The local protocol is not deleted because the callback
  shape is a real seam the upload pipeline depends on.
- This ADR exceeds the 250-line soft cap by user direction; the hard
  cap remains 500 lines. The expanded length is load-bearing: the
  decision shipping in this PR is the simultaneous adoption of three
  related changes (capability composition, note/mind-map split, and
  saved-chat ownership move) and they would not be coherent if split
  across multiple ADRs.

The 11-step incremental migration that realizes this decision is
detailed in `docs/refactor-history.md` §Migration Plan. It is intentionally
**not duplicated here**: this ADR records the architectural intent, not
the step sequence. Step ordering may evolve as the migration progresses
(e.g. when test fixture changes reveal a needed prior step), and the
authoritative ordering lives in `docs/refactor-history.md` until the cutover
lands.

### Composed Runtime Consequences

- **C-X. `cookie_saver` / `cookie_rotator` late-binding seams** — introduced by PR [#879](https://github.com/teng-lin/notebooklm-py/pull/879) (`76b301d`). The cookie persistence hooks are resolved dynamically during session lifecycle setup in `_session_lifecycle.py`, decoupling persistence logic from the core HTTP client transport and ensuring clean integration with the cookie keepalive loop.
- **C-Y. Inline `__Secure-1PSIDTS` cold-start recovery** — introduced by PR [#872](https://github.com/teng-lin/notebooklm-py/pull/872) / issue #865 (`6d8b5f4`). Production utilizes `_recover_psidts_inline` in `_auth/psidts_recovery.py` to run a preflight healing check. If preconditions are met (e.g. not bypassing credentials in environment-driven auth, and utilizing a flock-based cross-process lock), the system proactively mints a valid `__Secure-1PSIDTS` cookie before initializing the session facade to prevent cold-start failures.
- **C-Z. AST-based delegate-surface regression guard** — introduced by PR [#885](https://github.com/teng-lin/notebooklm-py/pull/885) (`f48d4b9`). To prevent erosion of the session-facade decoupling contract, a regression test in `tests/unit/test_session_compat_delegates.py` uses AST analysis to enforce that eight legacy/compatibility methods on the `Session` facade remain simple delegate forwards (6 `RpcExecutor`-adjacent and 2 `AuthRefreshCoordinator`-adjacent methods, each restricted to a maximum of 3 statements).
- **C-AA. Localized `DrainHookRegistration` Protocol** — The `DrainHookRegistration` protocol has been retired from the general `_session_contracts.py` surface and is now local to the `ArtifactsRuntime` in `_artifacts.py`.
- **C-AB. Narrow executor host Protocols after bridge retirement** — The session-shrink arc narrowed `RpcOwner` and `_AuthedTransportHost` after the legacy `Session` private-attribute shims were retired. `RpcOwner` no longer declares `_timeout`, `_refresh_callback`, `_refresh_retry_delay`, or `_http_client`; `RpcExecutor` receives those values through constructor-time providers or the concrete `Kernel`. `_AuthedTransportHost` no longer declares `_http_client` or `_bound_loop`; pre-open checks route through `Kernel.get_http_client()`, and loop affinity is asserted once in `Session._perform_authed_post`.

## Alternatives considered

1. **Keep the broad `Session` contract and let it continue to grow.**
   Rejected. The drift evidence is already visible at
   `_session_contracts.py:50`: ADR-010 specified five members, and the
   current contract has eight. Without an explicit promotion criterion
   (shared by ≥2 features), every future single-consumer capability is
   a candidate for promotion, and the contract is one PR away from
   nine members. The "narrow Session" intent of ADR-010 is not
   recoverable by exhortation; it requires a structural rule.

2. **Per-sub-client narrow Protocols only, no feature-local runtimes**
   (the post-D2 replacement from the ADR-002 lineage). Rejected.
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
   the result is the eight-member contract documented in
   `_session_contracts.py:50`. The promotion criterion in Decision §1
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
