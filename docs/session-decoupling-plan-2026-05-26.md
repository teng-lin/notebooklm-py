# Session Decoupling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete ADR-013's runtime decoupling. Make capability Protocols satisfied by collaborators or feature-local adapters at runtime — not by `Session`. Eliminate `RpcOwner`, remove `Session`'s compatibility forwards, finish the auth-facade split, and drain the ADR-007 allowlist as a *consequence* of the surface that previously pinned it being gone.

**Architecture:** Per [ADR-014](./adr/0014-feature-local-runtime-adapters.md): single-collaborator Protocols are satisfied by the collaborator directly; composite Protocols are satisfied by frozen-dataclass adapters that live next to the feature. `NotebookLMClient.__init__` is the composition root — it constructs the collaborator graph and wires each feature with the satisfier the feature needs.

**Tech Stack:** Python 3.10+, `asyncio`, `httpx`, frozen `dataclass`, `Protocol`, `mypy --strict`, `ruff`, `pytest` + `pytest-asyncio`, VCR.py cassettes, `tests/_fixtures/fake_core.make_fake_core` (ADR-007), `tests/_lint/` meta-tests.

---

## How to read this plan

- **Six waves**, each ships as one or more PRs. Waves 1–3 are mostly parallelizable; Wave 4 fans out per feature; Wave 5 is the cleanup; Wave 6 is documentation.
- Every **Task** is one PR. Steps within a task are bite-sized (2–5 minutes each).
- The plan supersedes the earlier `session-facade-reduction-plan-2026-05-26.md` (deleted in this commit). The earlier plan chased LOC targets, which is a symptom; this plan addresses the cause (runtime Protocol satisfaction).
- **Re-baseline before every task.** Recent merge cadence is high — `git pull && git log --oneline -10` and re-run the grep at the top of each task before writing code.
- **Rollback discipline.** Every wave's PRs are independently revertable. If a wave's merge introduces a regression caught post-merge, revert with `git revert <merge-sha>` and re-investigate before re-rolling. Collaborators retain their pre-wave shapes between PRs — there is no in-flight state that would prevent a revert. Task 1.1 (`RpcExecutor` rewiring + `RpcOwner` deletion) is intentionally a single PR (see Task 1.1 body for the rationale) — revert is per-PR, not per-task.

---

## Current state (verified 2026-05-26)

### What is true today

| Item | Evidence | Status |
|---|---|---|
| `RpcExecutor` consumes `_kernel`, `_perform_authed_post`, `_await_refresh`, `_increment_metrics` via `RpcOwner` | [`_rpc_executor.py:59-78,95,136,155,231,442`](../src/notebooklm/_rpc_executor.py) | Active leakage |
| `NotebookLMClient.__init__` passes `self._session` to every feature API | [`client.py:305-342`](../src/notebooklm/client.py) | The disconnect ADR-014 closes |
| `_session_init.build_collaborators` already returns a typed bundle | [`_session_init.py:224`](../src/notebooklm/_session_init.py) | Enables Wave 3 cleanly |
| `_source_upload.py` already takes `Kernel` + `AuthMetadata` as separate params | [`_source_upload.py:266-272`](../src/notebooklm/_source_upload.py) | Partial ADR-014 already in place |
| `auth.py` still hosts `load_auth_from_storage` body and `_validate_required_cookies` write-through | [`auth.py:167,245`](../src/notebooklm/auth.py) | Stage 6 not done |
| ADR-007 file-level allowlist | [`tests/_lint/test_no_forbidden_monkeypatches.py`](../tests/_lint/test_no_forbidden_monkeypatches.py) | **42 entries** (verified 2026-05-26 via `grep -c '^\s*"tests/' tests/_lint/test_no_forbidden_monkeypatches.py`). Roughly half pin Session-shaped surfaces (drainable by this plan); the remainder pin stdlib seams (`asyncio.to_thread`, `asyncio.sleep`), PSIDTS lock helpers, CLI resolver seams, and concurrency tests that legitimately need raw monkeypatching (out of scope). |

### What is already done (do not redo)

- rpc-dispatch Phases 1–7: `RpcExecutor.rpc_call`, `_execute_once`, decode-time retry inside executor, `Session.rpc_call` delegates, feature RPC callable consolidation — all landed (#1058, #1059, #1061).
- architecture-fix-plan Stage 2: RPC context vocabulary + guard test (#1049, #1025, #1061).
- architecture-fix-plan Stage 6 (partial): `AuthTokens` moved to `_auth/tokens.py` (#1055).
- `_session.py:__init__` factored into `_session_init.build_collaborators` (#1030).

### Out of scope

- Public API changes. `NotebookLMClient.rpc_call`, `.notebooks`, `.sources`, `.chat`, `.artifacts`, `.notes`, `.research`, `.settings`, `.sharing` keep their current signatures and return types. `AuthTokens`, the exception hierarchy, and the schemas in `types.py` are untouched.
- Wire-shape changes. RPC method IDs, encoder/decoder, cassettes — unchanged.
- Renaming `Session`. The class may eventually become `_SessionLifecycle` or similar after the migration, but that is a separate, smaller change deferred until after Wave 6.
- The TypeVar-on-`RpcCaller` work (code-quality lens suggestion). Tracked as a follow-up after Wave 6.

---

## Dependency map

```text
W0 baseline + ADR-014 land
              │
              ▼
W0.5 push Protocol-satisfying methods down to collaborators
   (operation_scope → TransportDrainTracker;
    register_drain_hook → TransportDrainTracker;
    assert_bound_loop → ClientLifecycle)
   — REQUIRED prereq for W1+ Rule 1 satisfaction
              │
              ▼
W1 collaborator decoupling ──┐
   (kill RpcOwner; rewire    │   parallel with W2
    RpcExecutor + any other  │
    "owner"-shaped pieces)   │
                             │
W2 Stage-6 auth split ───────┘   parallel with W1
              │
              ▼
W3 simple-feature direct collaborator wiring
   (Settings, Notebooks, Sources, Notes,
    Research, Sharing — receive RpcExecutor directly)
              │
              ▼
W4 composite-feature adapter migration
   (Chat, Artifacts, Upload — one PR each;
    introduce <Feature>RuntimeAdapter, rewire
    NotebookLMClient.__init__)
              │
              ▼
W5 Session forward removal + ADR-007 allowlist drain
   (forwards become removable; tests that
    pinned them migrate to fake-adapter
    construction in the same PR)
              │
              ▼
W6 documentation + ADR-014 status → Accepted
```

W1 and W2 are independent — different files, different reviewers, ship in parallel. W3 fans out to 6 small PRs that can also land in parallel once W1 is in. W4 is three sequential PRs (one per composite feature); each can absorb its slice of the ADR-007 allowlist drain. W5 collects whatever remains.

**Note on Wave 2 scope.** The auth-facade split is structurally orthogonal to the Session-decoupling axis — it closes ADR-003's deferred goal but is not required for ADR-014 Rules 1–6 to land. If schedule pressure makes it necessary, Wave 2 can ship as a separate plan (this plan's W1 + W0.5 + W3 + W4 + W5 + W6 still discharge the runtime decoupling). Keep it here when the team has bandwidth to take both axes together; pull it out otherwise.

---

## Wave 0: Pre-flight

### Task 0.1: Capture green baseline

**Files:** none

**Step 1:** Confirm clean working tree.

```bash
git status
git pull --ff-only
```

**Step 2:** Run the full pre-push verification.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/notebooklm
uv run pytest tests/unit tests/_lint -x -q
```

**Step 3:** Capture before-state metrics into the PR description (used at the end of every wave for delta reporting).

```bash
wc -l src/notebooklm/_session.py src/notebooklm/auth.py src/notebooklm/_rpc_executor.py
grep -c '^\s*"tests/' tests/_lint/test_no_forbidden_monkeypatches.py
grep -n "^class RpcOwner" src/notebooklm/_rpc_executor.py
```

**Done when:** All four checks pass green. Numbers recorded.

### Task 0.2: Land ADR-014

**Files:**
- Already created: `docs/adr/0014-feature-local-runtime-adapters.md` (Status: Proposed)
- Modify: `docs/adr/README.md` — add the ADR-014 row to the index table

**Step 1:** Confirm the ADR exists and matches the agreed shape.

```bash
ls docs/adr/0014-feature-local-runtime-adapters.md
head -5 docs/adr/0014-feature-local-runtime-adapters.md
```

**Step 2:** Add the index row in `docs/adr/README.md` after the ADR-013 row:

```markdown
| [0014](0014-feature-local-runtime-adapters.md) | Feature-local runtime adapters as Protocol satisfiers | Proposed |
```

**Step 3:** Commit.

```bash
git add docs/adr/0014-feature-local-runtime-adapters.md docs/adr/README.md
git commit -m "docs(adr): add ADR-014 feature-local runtime adapters as Protocol satisfiers"
git push
```

**Step 4:** Open PR. Request review from at least one architecture-area reviewer. **Block on this PR merging before any code starts** — the rest of the plan cites ADR-014 as authority.

**Done when:** ADR-014 merged on `main`, status still `Proposed` (will flip to `Accepted` at the end of Wave 6 when the migration is complete and the consequences are confirmed).

### Task 0.3: Create working branch

```bash
git checkout main && git pull --ff-only
git checkout -b session-decoupling
git push -u origin session-decoupling
```

PRs in Waves 1–6 target `session-decoupling`. The branch merges to `main` only after the full plan is green, or in earlier integration drops if waves complete cleanly.

---

## Wave 0.5: Push Protocol-satisfying methods down to collaborators

**Why this comes before Wave 1.** ADR-014 Rule 1 says single-collaborator
Protocols are satisfied by the collaborator directly. Currently that is
**false** for three Protocols: `operation_scope`, `register_drain_hook`, and
`assert_bound_loop` live on `Session`, synthesised from primitives the
collaborators expose. Wave 1+ cannot make `ClientLifecycle` satisfy `LoopGuard`
or `TransportDrainTracker` satisfy `OperationScopeProvider` /
`DrainHookRegistration` until the methods physically live there.

These are mechanical additive changes. Ship as one PR or three small ones; no
behavioural change beyond moving where the same code lives.

### Task 0.5a: Add `TransportDrainTracker.operation_scope(label)`

**Files:**
- Modify: [`src/notebooklm/_transport_drain.py`](../src/notebooklm/_transport_drain.py)
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py) — `Session.operation_scope` at line 495 becomes a one-line forward to `self._drain_tracker.operation_scope(label)` (note: Session attribute is `_drain_tracker`, NOT `_drain`)
- Modify: tests that monkeypatch `Session.operation_scope` — migrate to monkeypatch `TransportDrainTracker.operation_scope` if they need the substitution

**Step 1:** Copy the current `Session.operation_scope` async-context-manager body (lines 495–502 in `_session.py`) onto `TransportDrainTracker`. The body calls `begin_transport_post` and `finish_transport_post`, both already methods on `TransportDrainTracker` ([`_transport_drain.py:139,196`](../src/notebooklm/_transport_drain.py)). Internal calls become `self.begin_transport_post(label)` / `self.finish_transport_post(token)`. (Earlier plan drafts named these `begin_transport_task` — that is a different method; the actual current call is `begin_transport_post`.)

**Step 2:** Replace `Session.operation_scope` with a thin forward. **Important:** the existing `Session.operation_scope` carries `@asynccontextmanager` and `async def`. Returning `self._drain_tracker.operation_scope(label)` from inside that decorated function does NOT work — it would yield an async context manager, not enter it. Drop the `@asynccontextmanager` decorator on `Session.operation_scope` and change the signature to a plain sync function that returns the inner context manager:

```python
# _session.py — REPLACE the @asynccontextmanager async def operation_scope ...
def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
    """Forward to TransportDrainTracker.operation_scope (ADR-014 Rule 1)."""
    return self._drain_tracker.operation_scope(label)
```

Callers (e.g. `async with session.operation_scope(label):`) are unchanged — they work because `self._drain_tracker.operation_scope` returns the context manager from its own `@asynccontextmanager`.

**Step 3:** Run focused tests.

```bash
uv run pytest tests/unit -k "operation_scope or drain" -v
uv run pytest tests/unit/concurrency -v
```

**Step 4:** Commit.

```bash
git commit -am "refactor(drain): push operation_scope down to TransportDrainTracker (ADR-014 Rule 1 prereq)"
```

**Done when:** `TransportDrainTracker.operation_scope` exists and is callable; `Session.operation_scope` is a one-line forward; existing tests pass.

### Task 0.5b: Add `TransportDrainTracker.register_drain_hook(name, hook)` + move storage

**Files:**
- Modify: [`src/notebooklm/_transport_drain.py`](../src/notebooklm/_transport_drain.py) — add `register_drain_hook` method and the `_drain_hooks` storage
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py) — `Session.register_drain_hook` (line 421) becomes a one-line forward; `_drain_hooks` storage moves out
- Modify: `Session.close()` (or wherever drain hooks fire) — invoke through `self._drain_tracker.run_drain_hooks()` instead of reading `self._drain_hooks`

**Step 1:** Locate the storage. `grep -n "_drain_hooks" src/notebooklm/`. Move both the storage attribute and the firing site to `TransportDrainTracker`.

**Step 2:** Add `TransportDrainTracker.register_drain_hook(name, hook)` and `TransportDrainTracker.run_drain_hooks()` (or whatever the firing method is named).

**Step 3:** Update `Session.close()` to call `await self._drain_tracker.run_drain_hooks()` instead of iterating its own storage.

**Step 4:** Replace `Session.register_drain_hook` with `self._drain_tracker.register_drain_hook(name, hook)`.

**Step 5: Remove the `_drain_hooks` field from the `_LifecycleHost` Protocol** at [`_session_lifecycle.py:172-194`](../src/notebooklm/_session_lifecycle.py). After this task, `ClientLifecycle.close()` calls `await self._drain_tracker.run_drain_hooks()` and no longer reads `host._drain_hooks`. The Protocol surface shrinks accordingly.

**Step 6: Add explicit close-ordering tests** to `tests/unit/concurrency/` (or wherever drain-hook tests live):

- Register hooks A, B, C; close client; assert all three fired in registration order.
- Register a hook that raises; close client; assert other hooks still fire and the exception is logged but does not block close.
- Register a hook; never close; assert the hook never fires (no spurious calls).

These tests pin the close-ordering contract that the post-Wave-0.5 code must preserve, since the storage just moved to a new owner.

**Step 7: Focused tests + commit.**

```bash
uv run pytest tests/unit -k "drain_hook or drain" -v
uv run pytest tests/unit/concurrency -v
git commit -am "refactor(drain): push register_drain_hook + storage down to TransportDrainTracker; shrink _LifecycleHost"
```

**Done when:** drain hooks survive Session shutdown unchanged; `Session.register_drain_hook` is a forward; `_LifecycleHost._drain_hooks` is removed from the Protocol; close-ordering tests pass.

### Task 0.5c: Add `ClientLifecycle.assert_bound_loop()`

**Files:**
- Modify: [`src/notebooklm/_session_lifecycle.py`](../src/notebooklm/_session_lifecycle.py) — add `assert_bound_loop()` that calls `_loop_affinity.assert_bound_loop(self.get_bound_loop())`
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py) — `Session.assert_bound_loop` (line 486) becomes `self._lifecycle.assert_bound_loop()`

**Step 1:** Add to `ClientLifecycle`:

```python
def assert_bound_loop(self) -> None:
    """LoopGuard satisfaction (ADR-014 Rule 1)."""
    from ._loop_affinity import assert_bound_loop as _assert
    _assert(self.get_bound_loop())
```

**Step 2:** Replace `Session.assert_bound_loop` body with `self._lifecycle.assert_bound_loop()`.

**Step 3:** Focused tests + commit.

```bash
uv run pytest tests/unit/concurrency/test_loop_affinity_guard.py -v
git commit -am "refactor(lifecycle): push assert_bound_loop down to ClientLifecycle"
```

**Done when:** `ClientLifecycle` satisfies `LoopGuard` directly; `Session.assert_bound_loop` is a forward (deletable in Wave 5).

## Wave 1: Collaborator decoupling — kill the `_owner` pattern

### Task 1.0: Refactor `AuthRefreshCoordinator.await_refresh` to drop the host parameter

**Prerequisite** for Task 1.1. `AuthRefreshCoordinator.await_refresh(host)` currently takes `host: _AuthRefreshHost` to reach `auth`, `_metrics_obj`, `get_http_client()` ([`_session_auth.py:246,290`](../src/notebooklm/_session_auth.py)). When `RpcExecutor` stops holding a Session-shaped owner (Task 1.1), it cannot supply `host`. Either:

- **(a, preferred):** refactor `AuthRefreshCoordinator` to take `auth_metadata: AuthMetadata`, `metrics: ClientMetrics`, `kernel: Kernel` at construction; drop `host` from `await_refresh`'s signature; or
- **(b, fallback):** `Session` continues to be `host` for `await_refresh`; `RpcExecutor` calls `self._auth_refresh.await_refresh(self._session_host)` where `_session_host` is passed in at construction. This keeps `Session` in `AuthRefreshCoordinator`'s call surface even after Task 1.1.

**Choose (a)** for clean decoupling. (b) is acceptable as a transitional path if (a) is bigger than time allows; flag it as a Wave 7 follow-up if you ship (b).

**Files:**
- Modify: [`src/notebooklm/_session_auth.py`](../src/notebooklm/_session_auth.py)
- Modify: [`src/notebooklm/_session_init.py`](../src/notebooklm/_session_init.py) (AuthRefreshCoordinator construction)
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py) (callers of `_await_refresh`)
- Modify: focused auth-refresh tests

**Step 1:** Inspect *just `await_refresh`* (not other methods on the coordinator) and inventory the host attributes it reads.

```bash
# Limit to await_refresh's body specifically — other methods (snapshot, update_auth_tokens,
# update_auth_headers) also take host but are out of scope for this task.
awk '/async def await_refresh|def await_refresh/,/^    (async )?def |^class /' src/notebooklm/_session_auth.py
```

Verified at write time: `await_refresh` reads only `host._metrics_obj` (`_session_auth.py:290`). The broader `host.auth`/`host.get_http_client()` reads cited in earlier plan drafts belong to `snapshot`, `update_auth_tokens`, and `update_auth_headers` — **those methods keep `host` for now** and can be migrated in a Wave 7 follow-up if desired.

**Step 2:** For the (single) attribute `await_refresh` reads, identify the underlying collaborator (`_metrics_obj` → `ClientMetrics`).

**Step 3:** Migrate `AuthRefreshCoordinator.__init__` to accept those collaborators at construction. `await_refresh` becomes parameterless again.

**Step 4:** Update the `Session._await_refresh` forward (it calls `self._auth_coord.await_refresh(self)`) to `self._auth_coord.await_refresh()`.

**Step 5:** Focused tests.

```bash
uv run pytest tests/unit/test_refresh_state_machine.py tests/unit/test_session_auth.py tests/unit/test_refresh_lock_lazy_init.py -v
```

**Step 6:** Commit.

```bash
git commit -am "refactor(auth): AuthRefreshCoordinator takes constructed dependencies; await_refresh drops host"
```

**Done when:** `AuthRefreshCoordinator.await_refresh()` is parameterless; all callers updated; tests pass.

### Task 1.1: Rewire `RpcExecutor` to take direct collaborator dependencies

Depends on Task 1.0. `RpcExecutor` stops taking an `RpcOwner` and starts taking its actual collaborators directly.

**Files:**
- Modify: [`src/notebooklm/_rpc_executor.py`](../src/notebooklm/_rpc_executor.py)
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py) — current `RpcExecutor` construction is at [`_session.py:546`](../src/notebooklm/_session.py) inside `Session._get_rpc_executor` (lazy construction). Update that call site. (Note: the construction is NOT in `_session_init.py`.)
- Modify: [`tests/_fixtures/fake_core.py`](../tests/_fixtures/fake_core.py) (drop `RpcOwner` shape)
- Modify: [`tests/unit/test_rpc_executor.py`](../tests/unit/test_rpc_executor.py)
- Modify: [`tests/unit/test_session_contracts.py`](../tests/unit/test_session_contracts.py)

**Step 1: Write the failing test for the new constructor signature.**

```python
# tests/unit/test_rpc_executor.py — add at top of test file
def test_rpc_executor_constructs_with_direct_collaborators():
    """ADR-014 Rule 5: RpcExecutor takes Kernel/transport/auth_refresh/metrics directly."""
    kernel = MagicMock(spec=Kernel)
    transport = MagicMock(spec=SessionTransport)
    auth_refresh = MagicMock(spec=AuthRefreshCoordinator)
    metrics = MagicMock(spec=ClientMetrics)

    executor = RpcExecutor(
        kernel=kernel,
        transport=transport,
        auth_refresh=auth_refresh,
        metrics=metrics,
        decode_response=lambda *a, **k: None,
        is_auth_error=lambda e: False,
        sleep=asyncio.sleep,
        timeout_provider=lambda: 30.0,
        refresh_callback_enabled_provider=lambda: True,
        refresh_retry_delay_provider=lambda: 0.0,
    )
    assert executor._kernel is kernel
    assert executor._transport is transport
```

**Step 2: Run the test.** Expected: FAIL — `RpcExecutor.__init__` still takes `owner`.

```bash
uv run pytest tests/unit/test_rpc_executor.py::test_rpc_executor_constructs_with_direct_collaborators -v
```

**Step 3: Migrate `RpcExecutor.__init__`** to take the four collaborators as keyword-only parameters. **Do not** ship a transitional `owner=` compatibility path — it cannot be implemented cleanly (the proposed transitional code would set `self._transport = owner` then call `.perform_authed_post()`, but Session exposes `_perform_authed_post` with the underscore prefix, so the call would fail). Instead: migrate all `RpcExecutor(...)` construction sites and the `RpcOwner` Protocol in the same PR.

```python
# _rpc_executor.py — single keyword-only constructor, no compat path
class RpcExecutor:
    def __init__(
        self,
        *,
        kernel: Kernel,
        transport: SessionTransport,
        auth_refresh: AuthRefreshCoordinator,
        metrics: ClientMetrics,
        decode_response, is_auth_error, sleep,
        timeout_provider, refresh_callback_enabled_provider, refresh_retry_delay_provider,
    ):
        self._kernel = kernel
        self._transport = transport
        self._auth_refresh = auth_refresh
        self._metrics = metrics
        # ...rest unchanged
```

Because there is no transitional path, **Task 1.1 and Task 1.2 merge into a single PR**. The `RpcOwner` Protocol is deleted in the same change. This also means the `__all__` audit and downstream test migration (Task 1.2 Steps 0, 2, 3) happen in this PR — re-baseline before opening.

**Step 4: Update the dispatch methods to use the direct references.**

```python
# _execute_once
self._kernel.get_http_client()                       # pre-open guard
self._metrics.increment(rpc_calls_started=1)          # verify method name on ClientMetrics
response = await self._transport.perform_authed_post(...)

# try_refresh_and_retry — depends on Task 1.0 having landed
await self._auth_refresh.await_refresh()              # post-Task-1.0 parameterless signature
```

Verify each method name with `grep` before the migration:

```bash
grep -n "def increment\|def emit_rpc_event" src/notebooklm/_client_metrics.py
grep -n "def perform_authed_post" src/notebooklm/_session_transport.py
grep -n "def await_refresh\|async def await_refresh" src/notebooklm/_session_auth.py
```

If the underlying method has a different name from the Session forward, rename the underlying method to match the cleaner public name — no compatibility shims on the collaborator side.

**Step 5: Update the lazy-construction call site at `_session.py:546`** (`Session._get_rpc_executor`) to use the new keyword form:

```python
executor = RpcExecutor(
    kernel=self._kernel,
    transport=self._transport,
    auth_refresh=self._auth_coord,
    metrics=self._metrics,
    decode_response=self._decode_response,
    is_auth_error=self._is_auth_error,
    sleep=self._sleep,
    timeout_provider=lambda: self._lifecycle._timeout,
    refresh_callback_enabled_provider=lambda: self._auth_coord.has_refresh_callback,
    refresh_retry_delay_provider=lambda: self._refresh_retry_delay,
)
```

If `_session_init.py` also has a path that constructs `RpcExecutor` (run `grep -n "RpcExecutor(" src/notebooklm/`), update it the same way. As of the verified baseline, the construction is ONLY at `_session.py:546`.

**Step 6:** (intentionally blank — consolidated into Step 5 above)

**Step 7: Run focused tests.**

```bash
uv run pytest tests/unit/test_rpc_executor.py tests/unit/test_session_contracts.py tests/unit/test_authed_post_pipeline.py -v
```

**Step 8: Run the full unit + lint suite.**

```bash
uv run pytest tests/unit tests/_lint -x -q
uv run mypy src/notebooklm
```

**Step 9: Commit.**

```bash
git commit -am "refactor(rpc): RpcExecutor takes direct collaborator dependencies (ADR-014 Rule 5)"
```

**Done when (combined Task 1.1 + 1.2 PR):** `RpcExecutor` constructs from direct collaborators in production; `RpcOwner` Protocol is deleted; `_assert_session_satisfies_protocols` no longer asserts `RpcOwner`; downstream test pins (`test_session_contracts.py`, `test_tier_13_all_exports.py`, `test_session_compat_delegates.py`) updated; unit + lint suites green.

### Task 1.2 — folded into Task 1.1

The original Task 1.2 (deleting `RpcOwner` separately from the rewiring) is no longer a separate PR because no executable transitional `owner=` path exists (see Task 1.1 Step 3). The Task 1.2 steps below remain in the plan as a **checklist of additional work that lands in the same Task 1.1 PR** — do not split.

**Files (additional to Task 1.1):**
- Modify: [`src/notebooklm/_rpc_executor.py`](../src/notebooklm/_rpc_executor.py) (remove `RpcOwner`)
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py) (remove the `_assert_session_satisfies_protocols` clause for `RpcOwner`)
- Modify: [`tests/unit/test_session_contracts.py`](../tests/unit/test_session_contracts.py) (remove the satisfies-RpcOwner assertion)
- Modify: [`tests/_lint/test_no_forbidden_monkeypatches.py`](../tests/_lint/test_no_forbidden_monkeypatches.py) (remove allowlist entries that pinned `_rpc_call_impl` / `_perform_authed_post` if any survive)
- Modify: [`docs/architecture.md`](./architecture.md) (the "Executor protocols are narrow too" section)

**Step 0: Audit downstream `RpcOwner` importers** (so the deletion isn't silently breaking anything).

```bash
rg "^from .*import.*RpcOwner|^import.*RpcOwner|RpcOwner\b" src tests
```

Verified at write time, the importers are:
- `src/notebooklm/_session.py:50,54,60,65,690` — TYPE_CHECKING import + `_assert_session_satisfies_protocols` body
- `src/notebooklm/_session_transport.py:23` — docstring reference (no import)
- `tests/unit/test_tier_13_all_exports.py:46` — expects `RpcOwner` in `_rpc_executor.__all__`
- `tests/unit/test_session_compat_delegates.py:19,155,161` — pins narrowness assertion

All four must be updated or deleted in this PR.

**Step 1: Confirm no positional `RpcExecutor(owner)` callers remain after Task 1.1's keyword-only migration.**

```bash
rg "RpcExecutor\(\s*\w" src tests   # any positional first-arg call — should be empty after Task 1.1 Step 5
```

If hits remain at this point, the Task 1.1 migration was incomplete — go back and fix.

**Step 2: Update `_rpc_executor.__all__`** — remove `"RpcOwner"` from the list at `_rpc_executor.py:5`. Update `tests/unit/test_tier_13_all_exports.py:46` `EXPECTED_RPC_EXECUTOR_ALL` to match.

**Step 3: Update `tests/unit/test_session_compat_delegates.py`** — delete `test_session_satisfies_rpc_owner_protocol_members` (and its narrowness assertion).

**Step 4: Delete `RpcOwner` (the `owner=` parameter was never introduced per Task 1.1 Step 3).**

```python
class RpcExecutor:
    def __init__(
        self,
        *,
        kernel: Kernel,
        transport: SessionTransport,
        auth_refresh: AuthRefreshCoordinator,
        metrics: ClientMetrics,
        decode_response, is_auth_error, sleep,
        timeout_provider, refresh_callback_enabled_provider, refresh_retry_delay_provider,
    ):
```

Remove `class RpcOwner(Protocol):` block. Remove the `from ._kernel import Kernel` TYPE_CHECKING import if it was only for `RpcOwner`.

**Step 5: Remove `_assert_session_satisfies_protocols`** from `_session.py:53-66` (or strip just the `RpcOwner` line if other Protocols are checked there). Update the docstring at `_session.py:690` (the `_perform_authed_post` body that cites `RpcOwner` as the reason for staying on Session — after Rule 5, that reason no longer applies; rewrite to cite Rule 4's middleware-chain-seam rationale instead).

**Step 6: Update tests/docs.**

**Step 7: Run the full pre-push set.**

```bash
uv run ruff check .
uv run mypy src/notebooklm
uv run pytest tests/unit tests/_lint -x -q
```

**Step 8: Commit.**

```bash
git commit -am "refactor(rpc): delete RpcOwner Protocol (ADR-014 Rule 5 complete)"
```

**Done when:** `rg "RpcOwner" src tests` returns nothing (or only comment/historical references). `Session` no longer claims to satisfy an executor-private contract. `_rpc_executor.__all__` no longer exports `RpcOwner`.

### Task 1.3: Same exercise for any other `_owner`-shaped collaborator

**Investigation step.** Grep for the pattern:

```bash
rg "self\._owner|self\._core" src/notebooklm --type py
```

For each hit:
- If it's `RpcExecutor`, already done.
- If it's a different collaborator (likely candidates: `SessionTransport`, `ClientLifecycle`), repeat the Task 1.1 + 1.2 cycle as a separate PR.
- If it's a feature module reaching into a Session-shaped runtime, defer to Wave 3/4 where that feature is migrated.

**Done when:** No production collaborator in `_session_*.py` or `_rpc_executor.py` carries an `_owner` reference.

---

## Wave 2: Auth-facade split (Stage 6 of the original fix plan)

Run in parallel with Wave 1.

### Task 2.1: Move `load_auth_from_storage` body to `_auth/tokens.py`

**Files:**
- Modify: [`src/notebooklm/_auth/tokens.py`](../src/notebooklm/_auth/tokens.py)
- Modify: [`src/notebooklm/auth.py`](../src/notebooklm/auth.py)
- Test: [`tests/unit/test_auth_storage.py`](../tests/unit/) or the file currently exercising the function

**Step 1: Find existing tests.**

```bash
rg "load_auth_from_storage" tests --type py
```

**Step 2: Write a sibling test against the new private location.**

```python
# tests/unit/test_auth_tokens.py — add a new test
def test_load_auth_from_storage_accessible_from_private_module():
    """ADR-014 Rule 1 / Stage 6: storage loading lives in _auth/tokens.py."""
    from notebooklm._auth.tokens import load_auth_from_storage
    assert callable(load_auth_from_storage)
```

**Step 3: Run** — expected FAIL until move.

**Step 4: Move the body.** Cut `def load_auth_from_storage(...)` from `auth.py:245`, paste into `_auth/tokens.py` next to `AuthTokens`. Replace the `auth.py` body with a re-export:

```python
# auth.py
from ._auth.tokens import AuthTokens, load_auth_from_storage  # noqa: F401  -- public re-export
```

**Step 5: Run both tests + the public-shims pin.**

```bash
uv run pytest tests/unit/test_auth_tokens.py tests/unit/test_auth_storage.py tests/unit/test_public_shims.py -v
```

**Step 6: Commit.**

```bash
git commit -am "refactor(auth): move load_auth_from_storage to _auth/tokens.py"
```

**Done when:** body lives in `_auth/tokens.py`; `notebooklm.auth.load_auth_from_storage` still importable; pins green.

### Task 2.2: Invert `_validate_required_cookies` write-through (treat as a contract change)

This is the riskiest step in Wave 2 and the only **behaviour change** in the plan. The existing `auth._validate_required_cookies` at [`auth.py:167-198`](../src/notebooklm/auth.py) does **more** than write-through: it propagates module-level rebindings (`MINIMUM_REQUIRED_COOKIES`, `_EXTRACTION_HINT`, `_has_valid_secondary_binding`) from `auth.py` into `_auth.cookie_policy` *before* delegating, and mirrors warning state back *after*. `tests/unit/test_public_shims.py:1000–1030` pins both directions of this propagation. `_auth/cookies.py:33-40,244-252` has import-time policy aliases and call sites that depend on this contract.

**Strategy.** Make `auth._validate_required_cookies` a delegate to `_auth.cookie_policy._validate_required_cookies`. Migrate every test that currently monkeypatches `auth.MINIMUM_REQUIRED_COOKIES` (or related rebinds) to monkeypatch `_auth.cookie_policy.MINIMUM_REQUIRED_COOKIES` instead. The dual-write symmetry goes away because there's nothing to write through, *but the call sites that relied on the propagation must move first*.

**Treat this as a contract change, not a refactor.** Ship the behaviour-asserting tests before the implementation change, in a separate PR if needed.

**Files:**
- Modify: [`src/notebooklm/auth.py`](../src/notebooklm/auth.py) (collapse the multi-line body to a single re-export)
- Modify: [`tests/unit/test_public_shims.py`](../tests/unit/test_public_shims.py) (rewrite the symmetry tests to identity assertions)

**Step 1: Inventory every consumer of the propagation contract.**

```bash
# Direct attribute reads
rg "auth\.MINIMUM_REQUIRED_COOKIES|auth\._EXTRACTION_HINT|auth\._has_valid_secondary_binding" src tests

# Monkeypatch via setattr(object, name, ...) — same target, different syntax. Both must be migrated.
rg 'monkeypatch\.setattr\((auth|notebooklm\.auth),\s*"(MINIMUM_REQUIRED_COOKIES|_EXTRACTION_HINT|_has_valid_secondary_binding)"' src tests
rg 'monkeypatch\.setattr\("notebooklm\.auth\.(MINIMUM_REQUIRED_COOKIES|_EXTRACTION_HINT|_has_valid_secondary_binding)"' src tests

# All references to the function being inverted
rg "_validate_required_cookies" src tests
```

For each test that monkeypatches `auth.MINIMUM_REQUIRED_COOKIES` (or sibling rebinds), record the file and behaviour expectation. These tests must move to monkeypatching `_auth.cookie_policy.MINIMUM_REQUIRED_COOKIES` instead — that's the contract migration. Known site: `tests/unit/test_public_shims.py:1014` uses the `monkeypatch.setattr(auth, "X", ...)` form.

**Step 2: Write the new-contract regression tests *first*.** In a separate PR (or the first commit of this PR), add tests asserting:

- `auth._validate_required_cookies is _cookie_policy._validate_required_cookies` (identity).
- Every `_auth/cookies.py` call site that previously observed `auth.py`-level rebinding now observes `_auth.cookie_policy`-level rebinding (one test per behavioural path you found in Step 1).
- The mirror-back path (`_SECONDARY_BINDING_WARNED`) — if any test relied on `auth.py` *reading* the warning state set inside `_cookie_policy` — is replaced with direct reads from `_cookie_policy`.

Run them. Expected: **FAIL for the identity assertion** (the two implementations are distinct bodies pre-Step 5 — `auth._validate_required_cookies` has its own multi-line body at `auth.py:167` that does propagation; `_auth.cookie_policy._validate_required_cookies` is the inner implementation). FAIL also for the rebind-observation assertions. Both assertions pass only after Step 5 lands the re-export.

**Step 3: Confirm `_auth/cookie_policy.py` carries the canonical implementation.**

```bash
grep -n "def _validate_required_cookies" src/notebooklm/_auth/cookie_policy.py
```

If the bodies in `auth.py` and `_auth/cookie_policy.py` differ in observable behaviour, reconcile in a separate no-op commit *first*, with a focused test.

**Step 4: Migrate the rebind monkeypatch sites.** For every entry in the Step 1 inventory, change `monkeypatch.setattr(auth, "MINIMUM_REQUIRED_COOKIES", ...)` to `monkeypatch.setattr(cookie_policy, "MINIMUM_REQUIRED_COOKIES", ...)`. Do this *before* deleting the write-through — otherwise those tests fail without diagnostic context.

**Step 5: Replace the `auth.py` body with the re-export.**

```python
# auth.py — replace the multi-line _validate_required_cookies and the
# trailing "_auth_cookies._validate_required_cookies = ..." assignment
from ._auth.cookie_policy import _validate_required_cookies  # noqa: F401
```

Delete the `_auth_cookies._validate_required_cookies = _validate_required_cookies` line at `auth.py:198` — it is the write-through this task removes.

**Step 6: Run the public-shims + cookies tests.**

```bash
uv run pytest tests/unit/test_public_shims.py tests/unit/test_auth_cookies.py -v
```

Expected: the dual-write-symmetry tests at lines ~1000–1030 fail because the symmetry no longer exists. The Step 2 new-contract tests now PASS.

**Step 7: Replace the prior dual-write tests** with the identity check from Step 2 (delete the old ones).

**Step 8: Commit.**

```bash
git commit -am "refactor(auth): collapse _validate_required_cookies write-through to delegate"
```

**Done when:** `auth._validate_required_cookies` is one line; no write-through code in `auth.py`; tests green.

### Task 2.3: Audit the remaining `auth.py` surface

**Files:** investigation + small follow-up edits.

**Step 1:**

```bash
grep -n "^def \|^class \|^[A-Z_]* = " src/notebooklm/auth.py
```

**Step 2:** Classify each remaining top-level name:
- One-line re-export from `_auth/*` → leave it (this is the public adapter)
- Active body that should live in `_auth/*` → file a follow-up sub-task and migrate
- Constant that's part of the public surface → leave it

**Step 3: Update `CLAUDE.md`** — the `auth.py` row currently says the flat re-export goal is "deferred". After 2.1–2.3, edit that row to reflect the new state (re-export adapter; specific re-exports listed; no active orchestration).

**Step 4: Update `docs/adr/0003-auth-facade-write-through.md`** Status line to "Superseded — closed by ADR-014 + #<this PR>". Add a one-paragraph Consequences note: the goal was achieved by inverting the dependency (delegate to `_auth/cookie_policy`), not by physical re-export.

**Step 5: Commit.**

```bash
git commit -am "docs: close ADR-003 deferred goal; update auth.py row in CLAUDE.md"
```

**Done when:** `auth.py` body contains only imports, re-exports, `__all__`, and the documented compatibility helpers. ADR-003 Status reflects the close.

---

## Wave 3: Simple-feature direct collaborator wiring

After Wave 1, `RpcExecutor` satisfies `RpcCaller` directly. The simple features (Settings, Notebooks, Sources, Notes, Research, Sharing) declare an `RpcCaller` dependency in their constructors — they can take `RpcExecutor` directly instead of `Session`. Each feature is one small PR.

### Task 3.N (template, repeat per feature)

**Files (per feature, replace `<feature>` with the actual one):**
- Modify: [`src/notebooklm/client.py`](../src/notebooklm/client.py) (the constructor call for `<Feature>API`)
- Modify: tests that construct `<Feature>API` with a Session-shaped object
- Modify: `tests/_lint/test_no_forbidden_monkeypatches.py` if any allowlist entries for `<feature>` tests become removable

**Step 1: Find the construction site.**

```bash
grep -n "<Feature>API(" src/notebooklm/client.py
```

Today it's `self.<feature> = <Feature>API(self._session, ...)`.

**Step 2: Change to direct executor wiring.**

```python
self.<feature> = <Feature>API(self._session._rpc_executor, ...)
```

(Or, cleaner: expose `self._session.rpc_executor` as a public property of Session — see Task 3.0 below.)

**Step 3: Update tests** that constructed `<Feature>API(fake_session)`. Use `make_fake_core(rpc_call=...)` per ADR-007, which already returns a `FakeSession` satisfying `RpcCaller`. No fixture change needed; only call-site adjustment if the test previously reached into Session-specific attributes.

**Step 4: Drain matching allowlist entries.** If `tests/<feature>/...` was on the ADR-007 allowlist for monkeypatching Session-shaped behaviour, remove the entries.

**Step 5: Run focused tests.**

```bash
uv run pytest tests/unit/test_<feature>.py tests/_lint -v
```

**Step 6: Commit.**

```bash
git commit -am "refactor(<feature>): receive RpcExecutor directly per ADR-014 Rule 1"
```

### Task 3.0 (prerequisite to 3.1–3.6): Expose the collaborator bundle as a single typed attribute, plus two narrow accessors for late-bound collaborators

Per ADR-014 Rule 3 Stage A. Replaces the earlier 7-properties draft (rejected by oracle review — it re-created a discoverability hub on `Session`). One bundle accessor + two narrow late-bound accessors, all three lint-guarded in Wave 6.

**Why three accessors and not one:** the current `SessionCollaborators` dataclass at [`_session_init.py:92-109`](../src/notebooklm/_session_init.py) carries `metrics`, `drain_tracker`, `reqid`, `auth_coord`, `kernel`, `lifecycle`, `cookie_persistence`, `poll_registry` — **but not** `session_transport` (built later via `build_session_transport`, [`_session.py:398`](../src/notebooklm/_session.py)) and **not** `rpc_executor` (built lazily inside `Session._get_rpc_executor`, [`_session.py:546`](../src/notebooklm/_session.py)). Extending the dataclass with these two late-bound fields requires either (a) making it non-frozen and populating fields post-construction, or (b) moving `build_session_transport` and `RpcExecutor` construction into `build_collaborators` (which requires resolving their Session-dependent inputs first). Both are bigger than this task wants to be. Pragmatic answer: expose two narrow named accessors alongside the bundle accessor. Stage B (Wave 7) collapses all three into a single bundle by moving construction to `NotebookLMClient`.

**Files:**
- Modify: [`src/notebooklm/_session.py`](../src/notebooklm/_session.py)

**Step 1:** Store the bundle in `Session.__init__`. Today `_session.py:365` calls `build_collaborators(...)` and immediately unpacks fields onto Session attributes (lines 370–380), discarding the bundle. Add one line:

```python
# _session.py — after the build_collaborators call at line 365
collaborators = build_collaborators(...)
self._collaborators = collaborators        # NEW — store for the accessor
# ...existing per-field unpacking stays unchanged
```

**Step 2:** Add three read-only accessors:

```python
# _session.py — add near other accessors (around line 449)
@property
def collaborators(self) -> SessionCollaborators:
    """Typed access to the constructed collaborator bundle (ADR-014 Rule 3 Stage A).

    Stage A: ``NotebookLMClient.__init__`` reads ``self._session.collaborators.<field>``
    for feature wiring. Field set matches :class:`SessionCollaborators` at
    [`_session_init.py:92-109`](../src/notebooklm/_session_init.py) — including
    ``reqid`` (NOT ``reqid_counter``), ``metrics``, ``drain_tracker``, etc.

    Stage B (Wave 7): ``build_collaborators`` moves to :class:`NotebookLMClient`;
    this accessor and the two late-bound accessors below are deleted.
    """
    return self._collaborators

@property
def session_transport(self) -> SessionTransport:
    """Late-bound collaborator not present on :class:`SessionCollaborators` today
    (constructed via :func:`build_session_transport` *after* the bundle).
    Deleted with the rest of the accessors when Stage B lands.
    """
    return self._transport

@property
def rpc_executor(self) -> RpcExecutor:
    """Lazily-constructed collaborator not present on :class:`SessionCollaborators`
    today. Deleted with the rest of the accessors when Stage B lands.
    """
    return self._get_rpc_executor()
```

**Step 3:** Lint check that no other code path reads these as anything but read accessors:

```bash
rg "self\._collaborators\b|\.collaborators\b|\.session_transport\b|\.rpc_executor\b" src/notebooklm
```

**Step 4:** Commit.

```bash
git commit -am "feat(session): expose SessionCollaborators bundle + late-bound accessors (ADR-014 Rule 3 Stage A)"
```

**Done when:** `Session.collaborators`, `Session.session_transport`, `Session.rpc_executor` all return their typed objects; Wave 3/4 wiring uses `coll = self._session.collaborators` for base fields + `self._session.session_transport` / `self._session.rpc_executor` for late-bound; the Wave 6 lint guard (Task 6.3) treats all three as allowlisted only inside `client.py`, `_session.py`, and tests.

**Naming note for downstream tasks**: the bundle field is named `reqid` (not `reqid_counter`). Wave 4.1 wiring should read `coll.reqid`, not `coll.reqid_counter`.

### Task list for Wave 3

**Before Task 3.1, run the per-feature dependency audit.** Several feature APIs
do not take `RpcCaller` directly — they take a domain service that takes
`RpcCaller`. The migration target is the constructor of whatever object
currently receives `self._session`.

```bash
# For each feature, find what its constructor actually wants
grep -n "def __init__" -A 10 src/notebooklm/_notebooks.py src/notebooklm/_sources.py \
  src/notebooklm/_settings.py src/notebooklm/_sharing.py src/notebooklm/_research.py \
  src/notebooklm/_notes.py src/notebooklm/_note_service.py \
  src/notebooklm/_artifact_listing.py src/notebooklm/_artifact_downloads.py \
  src/notebooklm/_source_listing.py src/notebooklm/_source_content.py \
  src/notebooklm/_sharing_manager.py
```

Verified at write time (2026-05-26) — run the audit before each PR to re-confirm:

| Wave 3 PR | Construction site in `client.py` | Real `RpcCaller` consumer (verified) |
|---|---|---|
| 3.1 Settings | `SettingsAPI(self._session)` | `SettingsAPI(rpc: RpcCaller)` directly |
| 3.2 Sharing | `SharingAPI(self._session)` | `SharingAPI` (re-verify with `grep "def __init__" -A 5 src/notebooklm/_sharing.py` — likely direct `RpcCaller`) |
| 3.3 Research | `ResearchAPI(self._session)` | `ResearchAPI` (re-verify; if it takes `RpcCaller`, single-line wiring change) |
| **3.4 NoteService** | `NoteService(self._session)` via `NotesAPI(notes=NoteService(self._session), ...)` at `client.py:319` | **`NoteService(rpc: RpcCaller)`** (`_note_service.py:87`). `NotesAPI` itself takes `notes=NoteService`, `mind_maps=...`, `save_chat_answer=...` (`_notes.py:68-74`) — do **not** retarget `NotesAPI` |
| 3.5 Sources | `SourcesAPI(self._session, ...)` | `SourcesAPI(rpc: RpcCaller, ...)` directly (`_sources.py:103`) — single-line wiring change to pass `self._session.rpc_executor` (late-bound accessor per Task 3.0) |
| 3.6 Notebooks | `NotebooksAPI(self._session, sources_api=...)` | `NotebooksAPI(rpc: RpcCaller, ...)` directly (`_notebooks.py:145`) — single-line wiring change |

All Wave 3 changes are single-line wiring updates in `client.py` (the affected
constructor already takes `RpcCaller`) except 3.4 (Notes), where the
`NoteService(self._session)` construction is the target.

**Each PR template:**

1. Run `grep -n "def __init__" -A 10 src/notebooklm/<module>.py` to confirm the real consumer.
2. Change the construction site in `client.py` to pass `self._session.rpc_executor` (or relevant collaborator) to whatever object actually takes `RpcCaller`.
3. Update tests that constructed the consumer with a Session-shaped object.
4. Drain any matching ADR-007 allowlist entries.
5. Run focused + lint suites.
6. Commit per service / per feature family.

Order is by ascending complexity (Settings is smallest; Notebooks is biggest).
3.1–3.3 can land in parallel; 3.4–3.6 may stack on top of the smaller ones.

**Done when:** no `*API(self._session, ...)` or `*Service(self._session, ...)` calls remain in `client.py` (except the load-bearing `_session.lifecycle` / collaborator-graph access patterns documented in Wave 5).

---

## Wave 4: Composite-feature migration

Three features have composite-Protocol runtimes: Chat (`ChatRuntime`), Artifacts (`ArtifactsRuntime`), Upload (`UploadRuntime`). One PR per feature.

Per ADR-014 Rule 2's intent-based adapter threshold (introduce an adapter when a downstream consumer intentionally takes the composite as a single dependency, OR delegation changes the call shape, OR multiple consumers share the composite — the earlier numeric heuristic was replaced in round-2 amendments):

| Feature | Composite shape | Adapter? |
|---|---|---|
| Chat | `RpcCaller` + `LoopGuard` + `transport_post` + `next_reqid` (2 trivial delegates over 4 collaborators) | **No — pass collaborators directly to `ChatAPI`** (the trivial-1:1 case the ADR threshold rejects) |
| Artifacts | `RpcCaller` + `AsyncWorkRuntime` + `register_drain_hook` (3 capabilities, composite shape) | **Yes — `ArtifactsRuntimeAdapter`** |
| Upload | `RpcCaller` + `OperationScopeProvider` + `LoopGuard` (3 capabilities; `Kernel` + `AuthMetadata` already passed separately) | **Yes — `UploadRuntimeAdapter`** |

### Task 4.1: Wire `ChatAPI` with direct collaborator injection (no adapter); refactor `_chat_transport`; delete `ChatRuntime` Protocol

Per ADR-014 Rule 2 (intent-based threshold), `ChatRuntime` has no remaining consumer once `_chat_transport.chat_aware_authed_post` is refactored to take `SessionTransport` directly. Per Rule 2 Corollary, delete `ChatRuntime`.

**Three coupled changes ship as one PR** (or three commits on a stacked PR):
1. Refactor `_chat_transport.chat_aware_authed_post(runtime, ...)` → `chat_aware_authed_post(transport, ...)` taking `SessionTransport` directly.
2. Refactor `ChatAPI.__init__` to take collaborators directly.
3. Delete the `ChatRuntime` Protocol.

**Files:**
- Modify: [`src/notebooklm/_chat_transport.py`](../src/notebooklm/_chat_transport.py) — `chat_aware_authed_post(runtime, ...)` → `chat_aware_authed_post(transport, ...)`. Replace `runtime.transport_post(...)` with `transport.perform_authed_post(...)` plus the chat-specific `parse_label` handling.
- Modify: [`src/notebooklm/_chat.py`](../src/notebooklm/_chat.py) — `ChatAPI.__init__` signature: take collaborators (`rpc`, `transport`, `reqid`, `loop_guard`) instead of `ChatRuntime`. Internal `chat_aware_authed_post(runtime, ...)` callers (e.g. `_chat.py:330`) become `chat_aware_authed_post(self._transport, ...)`. **Delete the `ChatRuntime` Protocol** at `_chat.py:96-114` — no remaining consumer.
- Modify: [`src/notebooklm/client.py`](../src/notebooklm/client.py) — the `ChatAPI(...)` construction
- Modify: tests under `tests/unit/chat/` or wherever `ChatAPI` is constructed
- Modify: `tests/_lint/test_no_forbidden_monkeypatches.py` (drain chat-shape entries)

**Step 0: Refactor `_chat_transport.chat_aware_authed_post`** to take `SessionTransport` instead of `ChatRuntime`:

```python
# _chat_transport.py — new signature
async def chat_aware_authed_post(
    transport: SessionTransport,
    *,
    build_request: BuildRequest,
    parse_label: str,
) -> httpx.Response:
    """Chat-side semantic owner around SessionTransport.perform_authed_post.
    ADR-014 Rule 1: SessionTransport satisfies the transport surface directly;
    ChatRuntime is no longer required.
    """
    try:
        return await transport.perform_authed_post(
            build_request=build_request,
            log_label=parse_label,    # was passed as parse_label into runtime.transport_post
        )
    except TransportAuthExpired as exc:
        # ... existing chat-flavored exception mapping unchanged
        raise ChatError(...) from exc
```

Verify `SessionTransport.perform_authed_post` accepts the same kwargs that `runtime.transport_post` did. If the parameter shapes differ, add a thin shape-adaptation layer inside `chat_aware_authed_post` rather than carrying `ChatRuntime` forward.

**Step 1: Migrate `ChatAPI.__init__`** to take collaborators directly:

```python
# _chat.py
class ChatAPI:
    def __init__(
        self,
        *,
        rpc: RpcCaller,
        transport: SessionTransport,
        reqid: ReqidCounter,
        loop_guard: LoopGuard,
        conversation_cache: ConversationCache | None = None,
        notebooks: NotebookSourceIdProvider | None = None,
    ):
        self._rpc = rpc
        self._transport = transport
        self._reqid = reqid
        self._loop_guard = loop_guard
        # ... existing ConversationCache wiring
```

Replace every internal `self._runtime.X(...)` call with the direct collaborator: `self._rpc.rpc_call`, `self._reqid.next_reqid`, `self._loop_guard.assert_bound_loop`, and `chat_aware_authed_post(self._transport, ...)` for the chat-flavoured transport call (`chat_aware_authed_post` is a module function, not a SessionTransport method).

**Step 2: Delete the `ChatRuntime` Protocol from `_chat.py:96-114`.** No remaining consumer (the function refactor in Step 0 removed the last one).

**Step 3: Wire from `client.py`:**

```python
# client.py — replace
self.chat = ChatAPI(self._session, notebooks=self.notebooks)

# with — base fields through coll; late-bound through the two narrow accessors per Task 3.0
session = self._session
coll = session.collaborators
self.chat = ChatAPI(
    rpc=session.rpc_executor,             # late-bound; lazy
    transport=session.session_transport,  # late-bound; constructed after bundle
    reqid=coll.reqid,                     # base field (note: name is `reqid`, not `reqid_counter`)
    loop_guard=coll.lifecycle,
    notebooks=self.notebooks,
)
```

Verify the same pattern for ArtifactsRuntimeAdapter / UploadRuntimeAdapter
constructions in Wave 4.2 / 4.3 — those adapters take `rpc` (late-bound) and
collaborators that may also be base or late-bound. Re-check each against
`SessionCollaborators` field names before writing the constructor call.

**Step 4: Migrate `ChatAPI` test fixtures.** Tests that built a fake Session and patched chat-shape methods now construct `ChatAPI(rpc=fake_rpc, transport=fake_transport, ...)` with four narrow fakes. Each fake can be a `MagicMock(spec=RpcCaller)` etc.

**Step 5: Drain matching allowlist entries** from `tests/_lint/test_no_forbidden_monkeypatches.py`.

**Step 6: Run the full chat + lint suite.**

```bash
uv run pytest tests/unit/chat tests/_lint -v
```

**Step 7: Commit.**

```bash
git commit -am "refactor(chat): direct collaborator injection; delete ChatRuntime; rewrite chat_aware_authed_post (ADR-014 Rules 1+2)"
```

**Done when:** `ChatAPI` takes four named collaborators in its constructor; `chat_aware_authed_post` takes `SessionTransport`; `ChatRuntime` Protocol is deleted from `_chat.py`; chat tests use narrow fakes; no chat-shape entries remain on the ADR-007 allowlist.

### Task 4.2: Introduce `ArtifactsRuntimeAdapter`

Same shape as Task 4.1, scoped to `_artifacts.py`. The adapter additionally exposes `register_drain_hook()` and `operation_scope()`:

```python
@dataclass(frozen=True)
class ArtifactsRuntimeAdapter:
    rpc: RpcCaller
    drain: TransportDrainTracker
    lifecycle: ClientLifecycle

    async def rpc_call(self, *args, **kwargs):
        return await self.rpc.rpc_call(*args, **kwargs)

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        return self.drain.operation_scope(label)

    def register_drain_hook(self, name: str, hook) -> None:
        self.drain.register_drain_hook(name, hook)

    def assert_bound_loop(self) -> None:
        self.lifecycle.assert_bound_loop()
```

Replicate steps 1–9 from Task 4.1 against the artifacts surface.

### Task 4.3: Introduce `UploadRuntimeAdapter`

`SourceUploadPipeline` already takes `Kernel` and `AuthMetadata` as separate parameters
([`_source_upload.py:266-272`](../src/notebooklm/_source_upload.py)), so only the
`UploadRuntime` composite needs an adapter:

```python
@dataclass(frozen=True)
class UploadRuntimeAdapter:
    rpc: RpcCaller
    drain: TransportDrainTracker
    lifecycle: ClientLifecycle

    # methods mirror UploadRuntime exactly
```

Same migration shape. After Task 4.3, no feature receives `Session` at construction.

**Done when:** all three composite features run through their adapter. `grep "Session" src/notebooklm/client.py` shows only the construction of Session itself, the property accesses for the collaborator graph, and the lifecycle calls (`session.open()`, `session.close()`).

---

## Wave 5: Session forward removal + ADR-007 allowlist drain

After Wave 4, the **compatibility forwards** on `Session` (drain/metrics/operation_scope/kernel/authuser/save_cookies — the ones that exist only because features used to reach through `Session`) are reachable only by test code. They are removable.

**What stays on `Session` after Wave 5** (per ADR-014 Rule 4 retention list):

- `Session.rpc_call(...)` — **retained** as the public-API forward; pinned by `tests/unit/test_public_shims.py:1048-1089` because `NotebookLMClient.rpc_call` (the documented raw-RPC escape hatch) forwards through it. Internally now delegates to `self.rpc_executor.rpc_call(...)` (via the late-bound accessor added in Task 3.0).
- `Session.collaborators`, `Session.session_transport`, `Session.rpc_executor` — three typed accessors per Task 3.0 (Stage A of ADR-014 Rule 3). The first exposes the constructed bundle; the latter two expose late-bound collaborators not present on `SessionCollaborators` today. All three are deleted in Wave 7 when ownership moves to `NotebookLMClient`. Lint-guarded outside the composition root by `tests/_lint/test_client_composition.py` (Task 6.3).
- `open` / `close` / `_keepalive_loop` / `is_open` — lifecycle.
- `_authed_post_chain_terminal` — the live middleware chain leaf, wired via `wire_middleware_chain` in `_session_init.py:394-428`. Removing this breaks the chain.
- **Provider-closure capture targets** that `build_session_transport` and `wire_middleware_chain` reach through `host.X`. Note: capture mode varies — `refresh_callable=host._await_refresh` is a *bound-method* captured once at construction (not late-binding); `host._rate_limit_max_retries`, `host._server_error_max_retries`, `host._refresh_retry_delay`, `host.assert_bound_loop` are wrapped in lambdas and *are* late-bound. Either way, deleting any of these breaks the chain wiring.
- AST-guarded methods (`update_auth_tokens` etc. — see `test_concurrency_refresh_race.py`).

The Wave 5 inventory MUST classify each remaining method against this list. **Do not delete the middleware-chain seams.** A method is deletable only if it appears in NEITHER this retention list NOR the provider-closure capture targets above.

### Task 5.1: Inventory remaining forwards

**Files:** investigation only.

**Step 1:** List Session methods.

```bash
grep -n "^    async def \|^    def " src/notebooklm/_session.py | grep -v "^.*__init__\|^.*assert_bound_loop\|^.*open\|^.*close\|^.*is_open\|^.*_keepalive_loop"
```

**Step 2:** For each method, check whether anything in production calls it:

```bash
rg "session\.<method>\(|core\.<method>\(|_session\.<method>\(|host\.<method>\b" src/notebooklm
```

The `host.<method>` pattern is critical — `_session_init.py` provider closures capture `host._method` lambda-style; a `(` filter would miss these.

Categorize:
- **Pure forward, no production caller, not captured by `_session_init.py` provider closures** → delete (Task 5.2)
- **Pure forward, only test callers** → delete the method, migrate tests (Task 5.2)
- **AST-guarded by `test_public_shims.py` or `test_concurrency_refresh_race.py`** → keep, document why
- **Captured by a provider closure in `_session_init.py` (`wire_middleware_chain` / `build_session_transport`)** → KEEP — load-bearing live seam per ADR-014 Rule 4. Document the capture site.
- **Real orchestration** → keep

**Step 3:** Write the inventory as a **checked-in** `docs/session-method-retention.md`. This is NOT an ephemeral scratchpad; it becomes the document the next architecture refactor reads first. Format:

```markdown
# Session method retention (ADR-014 Rule 4)

| Method | Category | Disposition |
|---|---|---|
| `rpc_call` | public API forward | retain — pinned by `test_public_shims.py:1048-1089` |
| `_authed_post_chain_terminal` | middleware chain leaf | retain — wired by `_session_init.py:399` |
| `_await_refresh` | provider-closure capture target | retain — `refresh_callable=host._await_refresh` |
| `_rate_limit_max_retries` | provider-closure capture (late-bound) | retain |
| ... | ... | ... |
| `_perform_authed_post` | compatibility forward | delete in Task 5.2 |
| `_increment_metrics` | compatibility forward | delete in Task 5.2 |
| `operation_scope` | forward (after Wave 0.5a) | delete in Task 5.2 |
| ... | ... | ... |
```

**Step 4:** Add a `tests/_lint/test_session_retention.py` that AST-parses `_session.py`, enumerates the methods, and asserts every method is either listed in the retention doc OR matches a "delete in Task 5.2" disposition (i.e., the method hasn't been deleted yet). Any method added later that is not listed fails the lint at PR time. After Task 5.2 closes, the lint asserts every remaining method is in the "retain" set.

**Step 5:** Commit.

```bash
git add docs/session-method-retention.md tests/_lint/test_session_retention.py
git commit -m "docs+test(session): retention doc + lint guard (Wave 5 inventory)"
```

### Task 5.2: Delete forwards in clusters

Group the deletable methods into ~4-6 PRs by area (metrics cluster, drain cluster, transport cluster, auth cluster, kernel cluster, reqid cluster). For each cluster:

**Step 1:** Delete the methods.

**Step 2:** Update any test that called them. Tests should either call the collaborator directly (via the property added in Task 3.0) or use `make_fake_core` with a fake collaborator.

**Step 3:** Drain matching ADR-007 allowlist entries.

**Step 4:** Run focused + full pre-push.

**Step 5:** Commit per cluster.

```bash
git commit -am "refactor(session): delete <cluster> forwards (ADR-014 Rule 4)"
```

### Task 5.3: Final allowlist sweep

After 5.1–5.2, re-baseline:

```bash
grep -c '^\s*"tests/' tests/_lint/test_no_forbidden_monkeypatches.py
```

For each remaining entry, classify:
- Can be removed by extending `make_fake_core` → migrate, drop entry
- Pins a Session method that's still legitimate → leave with inline comment explaining why (e.g. "tests/unit/test_loop_affinity_violation.py — needs to break the contract on purpose")
- Stale entry (the method it pinned was deleted) → drop entry

**Done when:** allowlist is empty *or* every remaining entry has a one-line justification comment.

### Task 5.4: Update the retention doc as Task 5.2 PRs land

After each Task 5.2 cluster PR, move the deleted methods from the "delete in Task 5.2" rows of `docs/session-method-retention.md` to a `Deleted` section (or remove them entirely; preserve commit-SHA references in the section header). When all clusters land, the doc should show only the retention list. The lint test (`test_session_retention.py`) keeps it honest.

**No ephemeral file is created or deleted in this plan.** Earlier drafts proposed an `_audit.md` scratchpad — that became the checked-in retention doc above.

---

## Wave 6: Documentation + ADR status

### Task 6.1: Update `docs/architecture.md`

**Files:**
- Modify: [`docs/architecture.md`](./architecture.md)

**Step 1:** Rewrite the "Session as facade" section:
- Rename to "Session as lifecycle root"
- Replace the three-reason bullet (Public API, Protocol satisfaction, Test seams) with a description of what Session owns post-migration (lifecycle, collaborator graph, `NotebookLMClient.rpc_call` forward)
- Reference ADR-014

**Step 2:** Update the "Per-capability protocol model" section to note that feature APIs receive the adapter or collaborator that satisfies their Protocol — not Session.

**Step 3:** Update the "Executor protocols are narrow too" section — `RpcOwner` is gone; replace with "RpcExecutor takes its collaborators directly".

**Step 4:** Update the collaborator-graph diagram if it shows feature APIs depending on Session.

**Step 5:** Commit.

```bash
git commit -am "docs(architecture): reflect ADR-014 runtime decoupling"
```

### Task 6.2: Flip ADR-014 to Accepted (gated on follow-up issues)

**Files:**
- Modify: `docs/adr/0014-feature-local-runtime-adapters.md` (Status line, add Acceptance PR link)
- Modify: `docs/adr/README.md` (index status)

**Prerequisite — file two tracked GitHub issues BEFORE Wave 3 starts** (not at the end of Wave 6 — that's too late, and historically deferred goals never get picked up otherwise). Both issues must:

1. **Stage B (Rule 3 completion):** "Move `build_collaborators` ownership from `Session` to `NotebookLMClient`; delete `Session.collaborators` / `Session.session_transport` / `Session.rpc_executor` accessors and the `test_client_composition.py::test_stage_a_accessors_only_used_in_allowlist` allowlist."
2. **MiddlewareChainHost extraction (Rule 4 completion):** "Extract a `MiddlewareChainHost` collaborator that owns `_authed_post_chain_terminal` + the `_rate_limit_max_retries` / `_server_error_max_retries` / `_refresh_retry_delay` tunables; `Session` holds it like any other collaborator."

Both issues:
- Reference ADR-014 as motivation
- Link to the relevant Wave-7 entry in this plan
- Are linked from `docs/session-method-retention.md` (added in Task 5.1)
- Are linked from ADR-014 Status line after flipping to Accepted (`Accepted (#<final PR>; Stage-B issue #<N>; MiddlewareChainHost issue #<M>)`)

Without these tracked items, ADR-014's Rule 4 carve-out becomes the new gravity well (5 attributes survive on `Session` indefinitely "because the middleware chain needs them"), and the three Stage-A accessors become permanent fixtures.

**Step 1:** File the two GitHub issues. Capture their IDs.

**Step 2:** Change ADR Status from `Proposed (2026-05-26)` to `Accepted (#<final migration PR>; Stage-B issue #<N>; MiddlewareChainHost issue #<M>)`.

**Step 3:** Add a one-line Consequences confirmation: "Migration completed in #<PR list>; ADR-007 Session-shaped allowlist entries drained; Session reduced to lifecycle + retention list (see `docs/session-method-retention.md`)."

**Step 4:** Update `docs/adr/README.md` row to `Accepted`.

**Step 5:** Commit.

```bash
git commit -am "docs(adr): flip ADR-014 to Accepted after migration complete"
```

### Task 6.3: Add `tests/_lint/test_client_composition.py` AST guard

**Files:**
- Create: `tests/_lint/test_client_composition.py`

Protects against the most likely future drift: a new contributor under time pressure passes `self._session` to a new feature constructor "just for now". The lint catches it at PR time.

**Step 1:** Add two AST checks — one against passing `self._session` to feature constructors, one against reaching the Stage-A accessors from anywhere outside the allowlist:

```python
"""ADR-014 Rule 3 enforcement: features take collaborators, not Session."""
import ast
from pathlib import Path

CLIENT_PATH = Path("src/notebooklm/client.py")
FEATURE_API_NAMES = {
    "SettingsAPI", "SharingAPI", "ResearchAPI", "NotesAPI",
    "SourcesAPI", "NotebooksAPI", "ChatAPI", "ArtifactsAPI",
    "SourceUploadPipeline", "NoteService",
}
STAGE_A_ACCESSORS = {"collaborators", "session_transport", "rpc_executor"}
ACCESSOR_ALLOWLIST = {
    "src/notebooklm/client.py",
    "src/notebooklm/_session.py",
}  # plus all of tests/ — checked separately. _session_init.py is NOT
   # allowlisted: verified at write time, _session_init.py never reads
   # the Stage-A accessors (it constructs the collaborators that the
   # accessors expose, not the other way around).

def test_no_feature_constructed_with_session_at_composition_root():
    tree = ast.parse(CLIENT_PATH.read_text())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FEATURE_API_NAMES:
                for arg in node.args:
                    if isinstance(arg, ast.Attribute) and \
                       isinstance(arg.value, ast.Name) and \
                       arg.value.id == "self" and arg.attr == "_session":
                        violations.append(f"{node.func.id} at line {node.lineno}: passes self._session positionally")
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Attribute) and \
                       isinstance(kw.value.value, ast.Name) and \
                       kw.value.value.id == "self" and kw.value.attr == "_session":
                        violations.append(f"{node.func.id} at line {node.lineno}: passes self._session via kwarg {kw.arg}")
    assert not violations, "ADR-014 Rule 3 violations:\n  " + "\n  ".join(violations)

def test_stage_a_accessors_only_used_in_allowlist():
    """Stage A: Session.collaborators / session_transport / rpc_executor
    are *only* legitimate reads inside client.py / _session.py and tests/.
    Feature modules MUST NOT reach for them — they would re-establish
    Session as discoverability hub."""
    violations = []
    for src in Path("src/notebooklm").rglob("*.py"):
        rel = src.as_posix()  # rglob returns relative paths; use as_posix for matching
        if rel in ACCESSOR_ALLOWLIST:
            continue
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in STAGE_A_ACCESSORS:
                violations.append(f"{rel}:{node.lineno}: reads .{node.attr}")
    assert not violations, \
        "ADR-014 Rule 3 Stage-A accessor leak:\n  " + "\n  ".join(violations)
```

**Step 2:** Run.

```bash
uv run pytest tests/_lint/test_client_composition.py -v
```

**Step 3:** Commit.

```bash
git commit -am "test(lint): guard against feature constructors receiving Session (ADR-014 Rule 3)"
```

**Done when:** the test passes against the post-Wave-4 `client.py`. Any future change that passes `self._session` to a feature constructor will fail this lint at PR time.

### Task 6.4: Update CLAUDE.md

**Files:**
- Modify: [`CLAUDE.md`](../CLAUDE.md)

**Step 1:** Update the `Session as facade` mention. Update the `auth.py` row to reflect the closed write-through. Update the `_rpc_executor.py` row to reflect the deleted `RpcOwner`.

**Step 2:** Commit.

```bash
git commit -am "docs(claude): post-migration architecture notes"
```

---

## Final verification

After Wave 5 closes, before Wave 6:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/notebooklm
uv run pytest tests/unit tests/_lint
uv run pytest tests/integration
uv run pre-commit run --all-files
```

**Categorical targets (per ADR-014):**

| Target | Before (2026-05-26) | After |
|---|---|---|
| `RpcOwner` Protocol exists in `_rpc_executor.py` | yes (4 members) | no — deleted |
| Feature APIs and feature services receive `Session` at construction | yes (across `client.py:297-342`) | no — all receive adapter, collaborator, or collaborator-injected service |
| Session forwards reached from feature APIs / feature services | non-zero | 0 |
| Session compatibility forwards (the drain/metrics/operation_scope/kernel/authuser/save_cookies surface that exists only because features used to reach through `Session`) | non-zero | 0 |
| Session methods that are load-bearing middleware-chain seams (`_authed_post_chain_terminal`, the live provider-closure targets from `wire_middleware_chain` / `build_session_transport`) | present | **unchanged — retained by design per ADR-014 Rule 4** |
| ADR-007 forbidden-monkeypatch allowlist: entries that pinned a Session-shaped surface | non-zero | 0 |
| ADR-007 forbidden-monkeypatch allowlist: entries pinning unrelated seams (stdlib `asyncio.to_thread`, PSIDTS lock helpers, CLI resolver, real-time concurrency tests) | ~half of the 42 total | unchanged — each carries a one-line inline justification |
| ADR-003 status | "Superseded; flat re-export deferred" | "Superseded — closed" |
| ADR-013 runtime story | open | closed by ADR-014 |

The targets above replace the earlier draft's "0 forwards" and "0 allowlist
entries" overall — both of which were not achievable. The middleware-chain
provider closures in `_session_init.py:365-428` are intentional live seams
(see ADR-014 Rule 4), and the allowlist contains non-Session entries that
this plan does not address.

**LOC outcomes (predictions, not targets):**

| File | Before | Likely after | Reason |
|---|---|---|---|
| `src/notebooklm/_session.py` | 779 | ~350–450 | All forward bodies removed; lifecycle + collaborator graph + properties remain |
| `src/notebooklm/auth.py` | 397 | ~200–250 | Stage 6 split done; re-exports + documented compatibility helpers remain |
| `src/notebooklm/_rpc_executor.py` | 466 | ~440 | `RpcOwner` removed; constructor surface widened; net small change |
| `src/notebooklm/_chat.py` | 844 | ~835 | `ChatRuntime` Protocol deleted (~18 lines); `ChatAPI.__init__` widens slightly |
| `src/notebooklm/_artifacts.py` | 1041 | ~1065 | `ArtifactsRuntimeAdapter` adds ~25 lines |
| `src/notebooklm/_source_upload.py` | 911 | ~935 | `UploadRuntimeAdapter` adds ~25 lines |

Total source LOC change: ~−300 to −450 lines. LOC is incidental; the architectural change is the goal.

---

## What this plan does NOT do

Explicit constraints, named so scope cannot drift:

- **Does not delete `Session`.** Session survives as the lifecycle root and collaborator graph. The class may be renamed in a separate follow-up after the migration completes.
- **Does not change `NotebookLMClient` public surface.** Same constructor, same method names, same return types, same exception behaviour. Cassette-replaying tests pass unchanged.
- **Does not change shared capability Protocols.** `RpcCaller`, `LoopGuard`, `OperationScopeProvider`, `AuthMetadata`, `Kernel` keep their current shapes. ADR-014 changes who *satisfies* them at runtime, not what they require. **Exception:** the feature-local composite Protocol `ChatRuntime` is **deleted** by Wave 4.1 (per ADR-014 Rule 2 Corollary — no consumer remains once `_chat_transport.chat_aware_authed_post` takes `SessionTransport` directly). `ArtifactsRuntime` and `UploadRuntime` keep their shapes; they are satisfied by `ArtifactsRuntimeAdapter` / `UploadRuntimeAdapter` (Wave 4.2/4.3).
- **Does not auto-generate adapter forwards.** Adapter bodies are written explicitly to keep the Protocol contract visible at the satisfier.
- **Does not pursue the TypeVar-on-`RpcCaller` work.** That is a separate concern raised by the code-quality lens; tracked as a Wave 7 follow-up after the migration is complete.
- **Does not change Session's `_session_init.build_collaborators` shape.** That factoring already exists and is the right primitive for the new wiring.

---

## Risks

- **AST-guarded test pins around `tests/unit/test_public_shims.py` and `tests/unit/test_concurrency_refresh_race.py`.** These inspect Session source code or module identity. Wave 5 cluster-delete PRs need to migrate or update those guards in the same commit, or risk a green local run + red CI.

- **`make_fake_core` over-coupling.** Today the factory builds a wide `FakeSession`. After ADR-014, narrower fakes per feature are preferable. If extending `make_fake_core` for the new shape makes it >600 lines, split into per-domain factories (`fake_chat_runtime`, `fake_artifacts_runtime`, `fake_upload_runtime`, plus `fake_rpc_executor`) under `tests/_fixtures/`. Cross-loop-affinity tests legitimately need raw monkeypatching — they should keep their allowlist entries with documented justification.

- **Wave 4's three composite-feature PRs touch large files** (`_chat.py` 844 LOC, `_artifacts.py` 1041, `_source_upload.py` 911). Keep each PR scoped to adapter-introduction + wiring switch; do *not* combine with feature refactors. Reviewer cognitive load matters here.

- **Compatibility shims at `_artifacts.py:14-23` and `:67-74`** (the `_ARTIFACT_COMPAT_EXPORTS` and `_mind_map` re-exports). These exist for test monkeypatch convenience. They are out of scope for this plan but become easy to remove after Wave 5. File as Wave 7 follow-ups.

- **Wave 2 timing.** Run in parallel with Wave 1 only if the same engineer is *not* doing both. The auth-facade test pins are independent of executor work, but conflict resolution on `_session_init.py` (which constructs both `AuthRefreshCoordinator` and `RpcExecutor`) can get noisy if both waves touch it concurrently.

- **Integration tests with cassettes.** None of the planned changes touch wire shape or RPC dispatch semantics — cassettes should replay unchanged. Re-confirm at the end of every wave with `uv run pytest tests/integration -x -q`.

---

## Wave 7 follow-ups (out of scope; tracked for after this plan completes)

- **Move `build_collaborators` ownership to `NotebookLMClient`** (ADR-014 Rule 3 Stage B). `Session` takes `SessionCollaborators` as a constructor argument; `NotebookLMClient.__init__` calls `build_collaborators` and holds the bundle directly. Removes `Session.collaborators` accessor. Wider blast radius (every `Session(...)` test-construction site updates) — deferred so the in-flight ADR-014 migration stays bounded.
- **Extract a `MiddlewareChainHost` collaborator** that owns `_authed_post_chain_terminal` and the `_rate_limit_max_retries` / `_server_error_max_retries` / `_refresh_retry_delay` tunables. `Session` holds it like any other collaborator. Closes the chain-host coupling that Wave 5 documents but does not eliminate.
- TypeVar-parametrise `RpcCaller.rpc_call` and `RpcExecutor.rpc_call` so feature callers stop receiving `Any` at the RPC boundary (code-quality lens recommendation).
- Remove `_ARTIFACT_COMPAT_EXPORTS` and `_mind_map` re-exports from `_artifacts.py` (architecture-lens flagged).
- Split the 800+ LOC modules (`_artifacts.py`, `_chat.py`, `_source_upload.py`, `exceptions.py`) along documented seams.
- Rename `Session` to `_SessionLifecycle` or `_ClientCore` if the post-migration role is clearly lifecycle-only.
- Wire `tests/scripts/check_cassettes_clean.py` into `tests/_lint/` as a real pytest item (testing-lens recommendation).

---

## PR slicing summary

Recommended PR order, with parallelism:

| Wave | PR | Parallel with | Roughly |
|---|---|---|---|
| 0 | ADR-014 + index update | — | Day 1 |
| 1 | RpcExecutor direct collaborator rewiring + `RpcOwner` Protocol deletion (combined per Task 1.1/1.2 body) | W2 | Day 1–3 |
| 1 | Same exercise for any other `_owner`-shaped collaborator (if any survive) | W2 | Day 2–3 |
| 2 | `load_auth_from_storage` body to `_auth/tokens.py` | W1 | Day 1 |
| 2 | Invert `_validate_required_cookies` write-through | W1 | Day 2 |
| 2 | `auth.py` surface audit + ADR-003 close | W1 | Day 3 |
| 3 | Session collaborator properties (Task 3.0) | — | Day 3 |
| 3 | Migrate Settings / Sharing / Research / Notes / Sources / Notebooks (6 PRs) | each other | Day 4 |
| 4 | Chat direct collaborator injection + `_chat_transport` refactor + `ChatRuntime` deletion | — | Day 5 |
| 4 | `ArtifactsRuntimeAdapter` | — | Day 6 |
| 4 | `UploadRuntimeAdapter` | — | Day 7 |
| 5 | Session forward inventory + cluster deletions (4–6 PRs) | each other once W4 done | Day 8 |
| 5 | Final ADR-007 allowlist sweep | — | Day 9 |
| 6 | Update `docs/architecture.md`, CLAUDE.md, flip ADR-014 to Accepted | — | Day 9 |

Solo engineer estimate: ~2 weeks of focused work. Two engineers in parallel (one on W1+W3, one on W2+W4): ~1 week.
