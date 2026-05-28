# Naming Conventions

**Status:** Active
**Last Updated:** 2026-05-21

This document is the canonical reference for naming patterns that recur across
the `notebooklm-py` codebase. It catalogues three families that an internal
architecture audit (findings CC2 / CC3 / CC5) called out as inconsistent enough
to need a written tiebreaker:

1. [Waiting / polling verbs](#1-waiting--polling-verbs-cc2) — `poll_X` vs
   `wait_for_X` vs `wait_until_X` vs `await_X` vs `_wait_for_X`.
2. [RPC-callable Protocol names](#2-rpc-callable-protocol-names-cc3) —
   `NextCall` / `RpcCallback` / `RpcCaller`.
3. [Metrics method verbs](#3-metrics-method-verbs-cc5) — `record_X` vs `emit_X`.

Examples below cite **symbol names only** (no file:line refs). Use
`rg '<symbol>' src/notebooklm/` to locate the current home — line numbers drift
faster than this file can keep up with.

---

## 1. Waiting / polling verbs (CC2)

Five distinct verbs are intentional. They are not interchangeable. Pick the
shape that matches what the function actually does and the loop will document
itself.

### `poll_X` — one-shot status read, no loop, no sleep

A `poll_X` function performs **a single** status / readiness check and returns
immediately. It never sleeps and never iterates. Use this when the *loop* lives
in the caller (or in a `wait_*` wrapper) and the function is just the
per-iteration probe.

Examples:

- `ArtifactPollingService.poll_status` — single RPC list + scan for one task ID.
- `ArtifactsAPI.poll_status` — public single-shot facade over the service.
- `ResearchAPI.poll` — single status read for a research plan.
- `artifact_poll` (CLI command) — one shot, then exit. Use the separate
  `artifact wait` command for the blocking / looping variant; `artifact poll`
  itself has no `--wait` flag.

> **Test name:** "if I call this twice in a row without a sleep, does that make
> sense?" If yes → it's a `poll_X`.

### `wait_for_X` — bounded loop with a **timeout**

A `wait_for_X` function loops until either the awaited condition holds **or** a
deadline expires. Timeouts are required (default or explicit); the function
raises a typed `*TimeoutError` on expiry rather than returning a sentinel.

Examples:

- `ArtifactPollingService.wait_for_completion` — loops `poll_status` until the
  artifact is terminal or `timeout` elapses.
- `ArtifactsAPI.wait_for_completion` — public facade over the service loop.
- `ResearchAPI.wait_for_completion` — loops `poll` until research is terminal,
  pinning a discovered `task_id` between iterations.
- `SourcePoller.wait_for_sources` (and `SourcesAPI.wait_for_sources`) — batch
  wait across N source IDs with a shared deadline.
- `RetryMiddleware._wait_for_rate_limit` / `_wait_for_server_error` — private
  variant; see the underscore-prefix subsection below.

### `wait_until_X` — loop on a **predicate** (also bounded)

`wait_until_X` reads like English: "wait until `X` is true". Same loop+timeout
contract as `wait_for_X`, but the verb signals that the awaited condition is a
**state predicate** on a specific resource, not the *arrival* of a value.

Examples:

- `SourcePoller.wait_until_ready` / `SourcesAPI.wait_until_ready` — block until
  `source.is_ready`.
- `SourcePoller.wait_until_registered` / `SourcesAPI.wait_until_registered` —
  block until a freshly-added source appears in the notebook listing.

> **`wait_for_X` vs `wait_until_X`:** both loop with a timeout. The difference
> is naming ergonomics. Prefer `wait_until_X` when the awaited condition is a
> boolean property of an existing resource (`is_ready`, `is_registered`).
> Prefer `wait_for_X` when you're waiting on an external arrival or a *set* of
> items (`wait_for_sources`, `wait_for_completion`). Neither form is "more
> correct"; pick the one that reads naturally at the call site.

### `await_X` — coalesced single-flight join

`await_X` is reserved for **single-flight coalescing** primitives: many
concurrent callers join one shared in-flight operation. The function name
matches the user-facing verb ("await the refresh"), and the implementation
guarantees deduplication (typically via `asyncio.shield` + a stored task).

Examples:

- `AuthRefreshCoordinator.await_refresh` — thundering-herd-safe token refresh;
  all 401-bouncing callers join one refresh task.

Do **not** use `await_X` for ordinary `async def` functions just because they
get `await`-ed. The verb signals coalescing semantics, not async-ness.

### `_wait_for_X` — module-private backoff helper

The leading underscore + `wait_for_` shape is used inside middlewares to
indicate **"this is the bounded backoff helper I extracted from one specific
retry leg"**. It is not a public coordination primitive; it is a private
implementation detail of a larger retry loop.

Examples:

- `RetryMiddleware._wait_for_rate_limit` — honors `Retry-After`, falls back to
  exponential backoff. Called from inside the rate-limit branch of the retry
  loop; never called externally.
- `RetryMiddleware._wait_for_server_error` — same shape for the 5xx branch.

If you extract a backoff helper from a middleware, follow this pattern. If you
extract a *public* waiting primitive, drop the underscore and use one of the
four verbs above.

### Summary table

| Verb | Loop? | Timeout? | Predicate or arrival? | Shared single-flight? | Public? |
|---|---|---|---|---|---|
| `poll_X` | no (one-shot) | n/a | either | n/a | yes |
| `wait_for_X` | yes | required | arrival of value(s) | no | yes |
| `wait_until_X` | yes | required | state predicate | no | yes |
| `await_X` | yes (joins one task) | inherits from task | n/a | yes | yes |
| `_wait_for_X` | yes | required | arrival | no | no (module-private) |

---

## 2. RPC-callable Protocol names (CC3)

Most feature modules type their RPC dependency as the shared
`RpcCaller` object Protocol from `_session_contracts`. Only middleware-chain
callables and upload's keyword-injected registration callback keep local
callable shapes. These names are NOT interchangeable — the divergence is
structural, not stylistic. This section explains what each name signals so new
code picks the right shape.

### The three names in use

| Name | Defined in | Protocol shape | Used by |
|---|---|---|---|
| `NextCall` | `_middleware.py` | **type alias**, not a class: `Callable[[RpcRequest], Awaitable[RpcResponse]]` | Every `Middleware.__call__` — the "call the next link" function passed into around-style middlewares |
| `RpcCallback` | `_source_upload.py` | **Callable** Protocol: `async def __call__(method, params, ...)` | `SourceUploadPipeline.register_file_source` — RPC entrypoint passed as a **keyword argument** at call time |
| `RpcCaller` | `_session_contracts.py` | **Object** Protocol: `async def rpc_call(method, params, ...)` (i.e. `obj.rpc_call(...)`) | The canonical shared capability Protocol for pure-RPC feature APIs and helper services (`NotesAPI`, `SourceLister`, `ShareManager`, etc.) |

### Why they diverge

Two axes do the actual work:

1. **Callable Protocol vs Object Protocol.** `NextCall` and `RpcCallback` are
   *callable* shapes — the conformer is itself directly invokable
   (`rpc(method, params)`). `RpcCaller` is an
   *object* shape — the conformer exposes an `.rpc_call(...)` method
   (`executor.rpc_call(method, params)`).
   These are NOT interchangeable to mypy: a callable Protocol matches a bare
   function or `__call__`, while `RpcCaller` requires the named method. An
   `RpcExecutor` instance satisfies `RpcCaller` because it defines `rpc_call`; the
   bound method `executor.rpc_call` satisfies `RpcCallback` because it is a
   callable Protocol.
2. **Type alias vs Protocol class.** `NextCall` is a `Callable[...]` alias, not
   a class. It exists because the middleware chain is built from a list of
   wrapped callables (`functools.reduce`-style composition); a Protocol class
   would not buy anything over the alias and would make the middleware
   constructor signatures noisier.

`RpcCallback` exists separately from `RpcCaller` for one remaining reason:
it is a **keyword-only callback** passed into `register_file_source`, and
keeping it as a structural Protocol (instead of a bare `Callable[...]` alias)
lets mypy flag keyword-name typos at the call site.

### Choosing a name in new code

- New pure-RPC feature API? Type the dependency as
  **`RpcCaller`** from `_session_contracts`. This is the shared capability
  Protocol; see [`docs/architecture.md`](./architecture.md) for the protocol
  catalogue. Concrete `Session` satisfies it structurally.
- New middleware? Use **`NextCall`** from `_middleware.py` for the chain
  callable — do not invent a new alias.
- New feature that takes the RPC entrypoint as a **keyword argument** at call
  time? Define a local Protocol named **`RpcCallback`** so the keyword-typo
  detection kicks in at every call site.

> **Why not collapse the last callback too?** `SourceUploadPipeline` accepts
> `rpc_call=` as a keyword override inside `register_file_source(...)`; keeping
> a callable Protocol there preserves mypy's keyword-name checking at the
> override seam. Ordinary constructor-injected feature services should use
> `RpcCaller`.

This convention is guarded by
`tests/_lint/test_no_legacy_rpc_callable_aliases.py`: `RpcCall` and `ShareRpc`
must stay deleted, and `RpcCallback` must stay local to `_source_upload.py`.

---

## 3. Metrics method verbs (CC5)

`ClientMetrics` exposes two verb families. They have different threading and
back-pressure contracts; the verb is the contract.

### `record_X` — sync, mutates counter state under a lock

`record_X` methods are **synchronous**, take a numeric measurement (typically
seconds), and update an in-memory `ClientMetricsSnapshot` field under
`_metrics_lock`. They never call user code, never schedule tasks, and never
block on I/O. Callers do not need to be inside an event loop.

Examples (all on `ClientMetrics`):

- `record_rpc_queue_wait` — time waiting for the RPC semaphore.
- `record_upload_queue_wait` — time waiting for the upload semaphore.
- `record_lock_wait` — time waiting on `_reqid_lock` (or a similar internal
  lock).

Shared backend: `ClientMetrics._record_wait` (private; the three public
methods are typed wrappers around it).

> A `Session.record_upload_queue_wait` proxy exists too; it forwards to the
> `ClientMetrics` instance. The verb stays `record_*` because the contract is
> still sync + lock-protected counter mutation.

### `emit_X` — async, fires the user-supplied callback

`emit_X` methods are **`async def`** and `await` the user-configured telemetry
callback (`on_rpc_event=...`). They are the back-pressure seam: the awaited
callback can hold up the calling RPC if it does I/O, and that is intentional
(rate-limit feedback flows backwards into the producer).

Examples:

- `ClientMetrics.emit_rpc_event` — awaits the `on_rpc_event` callback with a
  `RpcTelemetryEvent` payload; swallows + logs callback exceptions so a
  misbehaving callback can't corrupt the RPC path.

Exceptions inside the callback are caught and logged (observability must not
alter behavior), but the `await` itself is load-bearing — *don't* fire-and-forget
the callback with `asyncio.create_task(...)`, because that would defeat the
back-pressure contract.

### Choosing a verb in new code

- The new method updates a counter / gauge / histogram bucket synchronously?
  Use **`record_X`** and document the unit (seconds, bytes, count).
- The new method dispatches an event to user code (callback, queue, log
  sink) and the producer should `await` it? Use **`emit_X`** and make it
  `async def`.
- If both apply (record *and* emit), do them as two calls: `record_*` first
  (cheap, lock-protected), then `await emit_*` (potentially slow, can raise —
  though `ClientMetrics.emit_rpc_event` swallows). Keep the verbs separate so
  the contract at each call site stays one line of code.

---

## Related documents

- [`docs/architecture.md`](./architecture.md) — the v0.5.0 collaborator graph,
  capability Protocols, and the `RpcCaller` catalogue entry.
- [`docs/development.md`](./development.md) — contributor on-ramp; this
  conventions doc is linked from its "Key Design Decisions" section.
- The architecture audit (findings CC2, CC3, CC5) that motivated this
  catalogue lives in the internal planning notes (`.sisyphus/plans/`, not
  checked in). Future codebase audits with naming-convention findings should
  extend this document rather than spawn parallel tiebreaker files.
