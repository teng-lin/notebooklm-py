# ADR-004: Loop-affinity contract for `NotebookLMClient`

## Status

Accepted (retroactive). Documents the contract shipped in the tier-7 thread-safety/concurrency arc; reaffirmed by the seam extractions in tier-8 and tier-10.

## Context

`NotebookLMClient` is an `async` client built on `httpx.AsyncClient` and a network of `asyncio` primitives — locks, semaphores, condition variables, queues, and a `Task` keepalive loop. All of those primitives bind to the event loop on which they are first awaited (or, for `asyncio.Lock` constructed without `loop=` on 3.10+, the loop running when they are first acquired).

Three failure modes appeared during the tier-7 audit:

1. **Cross-loop reuse.** A client opened on loop A, then awaited on loop B (e.g. a different `asyncio.run` invocation, or a different thread's loop). `asyncio.Lock.acquire()` from loop B on a lock owned by loop A either deadlocks (the wake-up is scheduled on a loop that will never run again) or raises a confusing `RuntimeError`, depending on the primitive.
2. **Cross-thread reuse.** Each OS thread has its own default loop. Sharing one `NotebookLMClient` instance across threads guarantees cross-loop reuse because each thread enters its own `asyncio.run`.
3. **Multi-tenant `AuthTokens` sharing.** The conversation cache (`ChatAPI._cache`) is per-instance for a reason: it keys on `notebook_id` but not on `account_email`, so sharing one client between two tenants would leak conversation IDs between accounts.

The audit chose the simplest possible contract: *one `NotebookLMClient` instance is bound to its `open()`-time event loop. Cross-loop, cross-thread, and cross-tenant reuse are unsupported.*

The contract is enforced at two layers:

- `src/notebooklm/_loop_affinity.py` exposes `assert_bound_loop(bound_loop)` which compares the current loop to the captured one and raises `RuntimeError` with an actionable diagnostic if they differ.
- `src/notebooklm/_core_lifecycle.py::ClientLifecycle.open()` captures the loop with `asyncio.get_running_loop()` and exposes it as `get_bound_loop()`. Every authed POST path forwards the captured loop into `assert_bound_loop()` before touching any loop-bound primitive.

`SessionCapabilities.bound_loop` (`_capabilities.py:248-274`) defensively returns `None` when `_lifecycle` is missing or returns a non-loop value, so `MagicMock`-backed test fixtures fall through to the silent no-op path instead of misclassifying a mock as a cross-loop call.

## Decision

One `NotebookLMClient` instance is bound to the event loop that ran `open()`. The contract is:

- **Cross-loop sharing is unsupported.** Re-using a client across `asyncio.run` invocations raises `RuntimeError` on the first authed POST.
- **Cross-thread sharing is unsupported.** Create one `NotebookLMClient` per thread.
- **Cross-tenant sharing is unsupported.** Each `AuthTokens` tenant gets its own `NotebookLMClient` instance — `ChatAPI._cache` is per-instance for exactly this reason.

The contract is enforced via `assert_bound_loop()` (raises `RuntimeError`) rather than via a defensive lock or a silent fallback. A noisy failure on the first violating call is strictly preferable to a deadlock or a leaked conversation ID.

`ClientLifecycle.get_bound_loop()` returns `None` before `open()` is called; the affinity helper treats `None` as a silent no-op so test fixtures that construct a client without opening it are not penalised.

## Consequences

**Wanted:**

- The failure mode is *fast and visible*. A cross-loop reuse fails on the first call, with a stack trace pointing at `assert_bound_loop`, not as a mysterious hang ten minutes later.
- The contract is *one sentence long*. New contributors do not need to learn six lifecycle rules — they need to learn one rule and one error message.
- Each seam (drain, auth refresh, keepalive, transport) can use plain `asyncio.Lock` / `asyncio.Semaphore` without defensive re-binding logic. The cost of cross-loop safety is paid once at the lifecycle layer, not in each seam.

**Unwanted:**

- Callers that *want* multi-loop / multi-thread reuse must construct multiple clients. For test code this is mildly verbose; for production code this is the right design (each loop owns its own connection pool) so the tax is paid in tests only.
- The loop-affinity bridge in `_capabilities.py:248-274` has to defensively handle `MagicMock`-shaped fixtures. The defensive code is small but it is a reminder that the contract is enforced via Protocol forwarding, not via a hard constructor invariant.
- The contract is *advisory* to multi-process callers. Processes do not share Python objects, so the contract is trivially satisfied across fork/spawn boundaries — but the diagnostic message says "loop", not "loop or process", so a reader of an error report has to know that processes are out of scope.

## Alternatives considered

- **Multi-loop support — rejected.** Would require every loop-bound primitive (lock, semaphore, transport, task) to be re-keyed per loop, plus a coordination layer to migrate state between loops. The complexity blowup is enormous and the use case (one Python process running multiple event loops on multiple threads, all sharing a single NotebookLM client) is not real for the project's audience.
- **Thread-safe synchronous client — rejected.** The library is fundamentally async because the underlying RPC surface is async-friendly and the user-facing patterns (polling artifact generations, streaming chat responses) are async-first. A sync wrapper is feasible but it would duplicate every API; the cost outweighs the niche benefit. Callers who need sync access can `asyncio.run(...)` per call.
- **Silent rebind on cross-loop access — rejected.** The naive fix is "if `bound_loop != current_loop`, just rebind". This is precisely the bug that creates cross-loop deadlocks: rebinding the *primitive* does not rebind the wake-up callbacks already queued on the old loop, and the old loop is by definition no longer running. The fast-fail rule is intentional.
- **Silent no-op on non-loop-bound primitives — partial alternative, applied.** The codebase distinguishes between primitives that *must* fail across loops (anything `asyncio.*`) and primitives that *can* tolerate cross-loop access (read-only dataclasses, metrics counters). The affinity check is applied at the entry points to the loop-bound primitives, not to every method. This is the chosen middle ground.
- **Per-instance event loop construction (the client owns its own loop).** Rejected. Would either spawn a thread per client (bad for resource scaling) or hide the loop ownership from the caller (bad for cancellation semantics). Letting the caller own the loop is the canonical async-Python pattern; the client only binds to whichever loop was running at open time.
