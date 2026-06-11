# ADR-0004: Loop-affinity contract for `NotebookLMClient`

## Status

Accepted (retroactive). Documents the contract shipped in the tier-7 thread-safety/concurrency arc; reaffirmed by the seam extractions in tier-8/tier-10 and the later `_runtime/` package split.

## Context

`NotebookLMClient` is an `async` client built on `httpx.AsyncClient` and a network of `asyncio` primitives — locks, semaphores, condition variables, queues, and a `Task` keepalive loop. All of those primitives bind to the event loop on which they are first awaited (or, for `asyncio.Lock` constructed without `loop=` on 3.10+, the loop running when they are first acquired).

Three failure modes appeared during the tier-7 audit:

1. **Cross-loop reuse.** A client opened on loop A, then awaited on loop B (e.g. a different `asyncio.run` invocation, or a different thread's loop). `asyncio.Lock.acquire()` from loop B on a lock owned by loop A either deadlocks (the wake-up is scheduled on a loop that will never run again) or raises a confusing `RuntimeError`, depending on the primitive.
2. **Cross-thread reuse.** Each OS thread has its own default loop. Sharing one `NotebookLMClient` instance across threads guarantees cross-loop reuse because each thread enters its own `asyncio.run`.
3. **Multi-tenant `AuthTokens` sharing.** The conversation cache is per-instance for a reason: it keys conversation turns by `conversation_id` and does not include `account_email`, so sharing one client between two tenants would leak conversation IDs/turns between accounts.

The audit chose the simplest possible contract: *an open `NotebookLMClient` instance is bound to its `open()`-time event loop. Open-client cross-loop, cross-thread, and cross-tenant reuse are unsupported. Closing and reopening the same instance on a new loop is supported; `open()` is the binding moment and resets loop-bound collaborators.*

The contract is enforced at two layers:

- `src/notebooklm/_loop_affinity.py` exposes `assert_bound_loop(bound_loop)` which compares the current loop to the captured one and raises `RuntimeError` with an actionable diagnostic if they differ.
- `src/notebooklm/_runtime/lifecycle.py::ClientLifecycle.open()` captures the loop with `asyncio.get_running_loop()` and exposes it as `get_bound_loop()`. It propagates the binding into collaborators that own loop-bound primitives.
- `src/notebooklm/_runtime/transport.py::RuntimeTransport.perform_authed_post()` calls the injected loop check before it enters the middleware chain and before any loop-bound primitive is touched.

`ClientLifecycle.get_bound_loop()` returns `None` before `open()` is called, and `assert_bound_loop(None)` is a silent no-op. That keeps fresh test fixtures from being misclassified as cross-loop calls before they have opened a transport. The shared capability-Protocol surface that feature APIs depend on lives in `src/notebooklm/_runtime/contracts.py` (`Kernel`, `RpcCaller`, `LoopGuard`). Single-consumer protocols stay local to their owners, such as `AuthMetadata` in `src/notebooklm/_source/upload.py` and `OperationScopeProvider` in `src/notebooklm/_artifact/polling.py`.

## Decision

One `NotebookLMClient` instance is bound to the event loop that ran `open()`. The contract is:

- **Open-client cross-loop sharing is unsupported.** Re-using an already-open client across `asyncio.run` invocations raises `RuntimeError` on the first authed POST. Close → reopen on a different loop is supported and rebuilds/resets loop-bound primitives.
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
- The pre-open `None` path exists for fixture ergonomics, so the constructor does not enforce a hard loop invariant. The contract is enforced at async entry points that touch transport or loop-bound primitives.
- The contract is *advisory* to multi-process callers. Processes do not share Python objects, so the contract is trivially satisfied across fork/spawn boundaries — but the diagnostic message says "loop", not "loop or process", so a reader of an error report has to know that processes are out of scope.

## Alternatives considered

- **Multi-loop support — rejected.** Would require every loop-bound primitive (lock, semaphore, transport, task) to be re-keyed per loop, plus a coordination layer to migrate state between loops. The complexity blowup is enormous and the use case (one Python process running multiple event loops on multiple threads, all sharing a single NotebookLM client) is not real for the project's audience.
- **Thread-safe synchronous client — rejected.** The library is fundamentally async because the underlying RPC surface is async-friendly and the user-facing patterns (polling artifact generations, streaming chat responses) are async-first. A sync wrapper is feasible but it would duplicate every API; the cost outweighs the niche benefit. Callers who need sync access can `asyncio.run(...)` per call.
- **Silent rebind on cross-loop access — rejected.** The naive fix is "if `bound_loop != current_loop`, just rebind". This is precisely the bug that creates cross-loop deadlocks: rebinding the *primitive* does not rebind the wake-up callbacks already queued on the old loop, and the old loop is by definition no longer running. The fast-fail rule is intentional.
- **Silent no-op on non-loop-bound primitives — partial alternative, applied.** The codebase distinguishes between primitives that *must* fail across loops (anything `asyncio.*`) and primitives that *can* tolerate cross-loop access (read-only dataclasses, metrics counters). The affinity check is applied at the entry points to the loop-bound primitives, not to every method. This is the chosen middle ground.
- **Per-instance event loop construction (the client owns its own loop).** Rejected. Would either spawn a thread per client (bad for resource scaling) or hide the loop ownership from the caller (bad for cancellation semantics). Letting the caller own the loop is the canonical async-Python pattern; the client only binds to whichever loop was running at open time.
