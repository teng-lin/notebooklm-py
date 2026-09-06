# Operation Deadlines, Ownership, and Recovery Contracts

**Status:** Active
**Last Updated:** 2026-09-05

This guide explains how `NotebookLMClient` bounds multi-call work, attributes
cancellation, records mutation evidence, and tells callers what is safe to do
after a partial or ambiguous result. These contracts are backend-neutral: Web
and Android keep different wire implementations, but expose the same ownership
and recovery vocabulary.

Use the explorable
[deadline and cancellation sequence](https://teng-lin.github.io/notebooklm-py/diagrams/36-operation-deadline-and-cancellation.html)
and
[journal and recovery data flow](https://teng-lin.github.io/notebooklm-py/diagrams/37-operation-journal-and-recovery.html)
alongside this guide.

## One deadline for a whole workflow

`RuntimeOptions.operation_timeout` is the optional default aggregate budget for
first-party operations. Its default is `None`, which leaves aggregate operation
time unbounded while preserving every existing RPC, transfer, retry, and poll
timeout. `client.operation(timeout=...)` creates an explicit aggregate scope:

```python
from notebooklm import NotebookLMClient, OperationTimeoutError
from notebooklm.options import ClientConfig, RuntimeOptions

config = ClientConfig(runtime=RuntimeOptions(operation_timeout=120.0))

async with NotebookLMClient.from_storage(config=config) as client:
    try:
        async with client.operation(timeout=30.0):
            notebook = await client.notebooks.create("Quarterly review")
            await client.sources.add_url(notebook.id, "https://example.com")
    except OperationTimeoutError as error:
        # Evidence from every mutation already attempted in this scope is retained.
        inspect(error.operation_metadata)
```

The scope stores one absolute monotonic deadline. Each call boundary computes a
fresh remaining budget and uses the earlier of that operation deadline and its
own phase deadline. Queue waits, auth work, retry sleeps, transport calls,
reconciliation, polling waiters, and transfers therefore cannot reset the
aggregate clock. A shorter phase timeout still wins.

Nested scopes can only shorten the parent deadline. At the top level,
`client.operation(timeout=None)` explicitly disables the configured aggregate
default. Inside an already bounded scope, `timeout=None` still inherits the
parent absolute deadline; it cannot widen or remove it.

Expiry stops local waiting and blocks new dispatch. It does not revoke a write,
artifact generation, or research job that the upstream service already
accepted. Cancellation-safe local settlement may also extend the tail slightly
so admission tokens and mutation evidence are not lost.

## Task, loop, and client-generation ownership

An operation context belongs to three identities at once:

| Owner | Contract |
|---|---|
| `asyncio` task | Only the task that entered the scope may consume its context. A context copied into an arbitrary `asyncio.create_task()` child is ignored. |
| Event loop | The context uses the loop that admitted it. Cross-loop use fails before loop-bound transport state is touched. |
| Client epoch | The context is fenced to the open client resource generation. Work from a retired epoch cannot dispatch through a reopened client. |

Library-owned exclusive child tasks are registered through `CallSupervisor`.
They receive a child-owned context with the same deadline and journal only when
the caller explicitly chooses operation inheritance. Detached shared producers
clear both the operation context and journal bindings.

This distinction matters for application code: `client.operation(...)` is a
same-task scope. Do not assume that arbitrary tasks created inside it inherit
the deadline. Give independent tasks their own explicit operation scopes and
budgets.

## Cancellation attribution

The operation timer requests cancellation of its owning task, then translates
only that owned request into `OperationTimeoutError`. The runtime keeps the
other termination paths distinct:

| Event | Observable result |
|---|---|
| Aggregate deadline fires with no competing cancellation | `OperationTimeoutError`, including the journal snapshot captured so far |
| Caller, `TaskGroup`, or outer timeout cancels the task | The original `asyncio.CancelledError` propagates |
| Client generation is retired while the task unwinds | Cancellation propagates; stale work is not mislabeled as a deadline |
| Deadline and external cancellation arrive in the same event-loop turn | External cancellation wins; on Python 3.11+ the runtime removes only its own cancellation request |

Code must not catch `CancelledError` and retry a mutation. Cancellation says
why the local task stopped; `commit_state` says what is known about the remote
write. Those are separate axes.

### Detached shared polling

Artifact completion polling has leader/follower ownership. Each waiter is
admitted independently, and its own enclosing aggregate operation budget limits
how long that waiter may remain attached. The first waiter creates a registered
leader task outside the waiter's operation context; followers attach to the same
future through `asyncio.shield`. Cancelling or timing out one waiter therefore
detaches that waiter without cancelling the shared leader or other followers.
Client drain still owns the leader and cancels and gathers it before transport
teardown.

The first waiter's polling knobs govern that shared detached leader: initial and
maximum intervals, `wait_for_completion(timeout=...)`, not-found count, and
not-found window. As enforced by
[`_artifact/polling.py`](../src/notebooklm/_artifact/polling.py), a later
follower's differing polling knobs are currently ignored and emit a registered
deprecation warning. The leader's timeout controls its retry backoff and
terminal pending/in-progress errors; it is not a separate per-follower phase
budget.

## The mutation journal

Each top-level workflow owns one private `OperationJournal`. Nested operation
scopes and exclusive library child tasks that explicitly inherit the operation
context share that journal and its ordered entries. A `JournalEntry` represents
a semantic send, keyed by a stable `SendIdentity` containing the invocation,
operation, method, phase, and optional batch-member occurrence. An entry appends
one `AttemptRecord` for every physical dispatch, including auth or transport
retry attempts.

Dispatch begins conservatively as `UNKNOWN`. Positive evidence can settle the
attempt, but a later success cannot erase an earlier ambiguous attempt. The
public snapshot uses four states:

| `CommitState` | What is proven | Safe default |
|---|---|---|
| `NOT_SENT` | The producer has positive evidence that the attempt did not dispatch | Retry only when the operation owner explicitly authorizes it |
| `REJECTED` | A decoded response proves the service refused the mutation | Correct the request, or retry only under explicit owner policy |
| `UNKNOWN` | A dispatched write may have committed, but no correlated result proves either outcome | Inspect and reconcile; never blindly replay |
| `CONFIRMED` | A correlated response or authoritative readback proves the mutation committed | Continue from the confirmed resource IDs |

A workflow snapshot aggregates mutation leaves conservatively in this order:
`UNKNOWN`, then `CONFIRMED`, then `REJECTED`, then `NOT_SENT`. Baseline,
readback, observation, cleanup, and wait entries remain visible but do not
override mutation certainty when mutation entries exist. If an exception
already identifies the escaping send, aggregation preserves that leaf as the
primary identity while retaining every entry, attempt, and proven resource ID.

`OperationMetadata` is the immutable, redaction-safe public carrier attached to
`NotebookLMError.operation_metadata`. It contains the aggregate state, semantic
identity, proven IDs, reconciliation report, batch outcome, recovery action,
and bounded per-entry evidence. Exception properties such as `commit_state`,
`batch_outcome`, `source_id`, `stage`, and legacy `unconfirmed` are projections
of this one carrier, not separate authorities.

## Complete batch outcomes

`SourcesAPI.add_urls_batch()` returns one `SourceBatchItemOutcome` for every
input occurrence in original order. Duplicate URLs remain distinct because
`member` is an occurrence index. Each item carries a canonical
`BatchItemOutcome`:

- `CONFIRMED` has a matching `resource_id` and public `Source`.
- `REJECTED` has a typed error and cannot claim a resource.
- `UNKNOWN` has a `ReconciliationReport`; candidate rows are not success proof.
- `NOT_SENT` has positive zero-send evidence and cannot claim a resource.

If a backend call, adapter projection, or cancellation escapes after partial
progress, the exception's `batch_outcome` still contains a complete ordered
settlement. Local validation failures are merged with backend-relative member
indexes; missing valid members fail closed as `UNKNOWN`. Public inputs and
adapter payloads are capped and redact URL credentials and sensitive query
values.

`whole_request_retriable=True` is allowed only when no member is `CONFIRMED` or
`UNKNOWN`. Prefer per-member continuation over whole-batch replay.

## Recovery actions

`RecoveryAction` is a producer-owned continuation hint. It deliberately does
not derive from HTTP status, gRPC status, exception category, or presentation
text.

| Action | Meaning |
|---|---|
| `RETRY` | The owning operation has enough positive evidence to authorize another send |
| `INSPECT_AND_RECONCILE` | State may exist remotely; inspect candidates and correlate manually before any new send |
| `WAIT` | A known upstream resource or processing phase should be observed rather than recreated |
| `NONE` | No generic recovery action is asserted; use the domain result and evidence |

`known_resource_ids` contain proven handles. `ReconciliationCandidate` values
are bounded suggestions only and never become proven IDs without authoritative
correlation. This separation prevents a title or URL near-match from being
reported as the result of the current invocation.

CLI, MCP, and REST adapters project this neutral carrier. They may choose their
own presentation, status code, or envelope, but must preserve commit state,
ordered batch members, recovery action, and redaction. Adapter classification
cannot upgrade ambiguous evidence into a retryable failure or success.

## Evidence and guardrails

The implementation and its most direct tests are pinned at revision
`719378b8af7023b8eca0c33d2de6d44d35db3434`:

| Contract | Implementation | Tests |
|---|---|---|
| Scope ownership and cancellation attribution | [`_runtime/operation_context.py`](../src/notebooklm/_runtime/operation_context.py), [`_runtime/call_supervisor.py`](../src/notebooklm/_runtime/call_supervisor.py) | [`test_operation_context.py`](../tests/unit/test_operation_context.py) |
| Earlier phase/operation deadline | [`_deadline.py`](../src/notebooklm/_deadline.py), [`_runtime/call_supervisor.py`](../src/notebooklm/_runtime/call_supervisor.py) | [`test_operation_context.py`](../tests/unit/test_operation_context.py) |
| Detached shared polling | [`_artifact/polling.py`](../src/notebooklm/_artifact/polling.py), [`_polling_registry.py`](../src/notebooklm/_polling_registry.py) | [`test_artifact_polling_paths.py`](../tests/unit/test_artifact_polling_paths.py), [`test_artifacts_polling_retries.py`](../tests/unit/test_artifacts_polling_retries.py) |
| Journal and recovery vocabulary | [`_idempotency.py`](../src/notebooklm/_idempotency.py), [`outcomes.py`](../src/notebooklm/outcomes.py) | [`test_operation_journal.py`](../tests/unit/test_operation_journal.py) |
| Complete source-batch settlement | [`_source/batch.py`](../src/notebooklm/_source/batch.py), [`_web/sources/batch.py`](../src/notebooklm/_web/sources/batch.py), [`_android/source_batch.py`](../src/notebooklm/_android/source_batch.py) | [`test_source_batch_outcomes.py`](../tests/unit/test_source_batch_outcomes.py), [`test_source_batch_parity.py`](../tests/server/test_source_batch_parity.py) |
| Structural ownership inventory | [`test_client_operation_contract_inventory.py`](../tests/_guardrails/test_client_operation_contract_inventory.py) | The inventory is executable and rejects unowned migration rows |
| Typed facade boundary | [`test_no_raw_positional_rpc_indexing.py`](../tests/_guardrails/test_no_raw_positional_rpc_indexing.py) | Raw payload ingress above the facade and unbaselined positional decoding fail CI |

The updated
[retry workflow](https://teng-lin.github.io/notebooklm-py/diagrams/20-retry-policy-workflow.html)
shows how backend replay classification composes with the deadline and journal;
the
[guardrail map](https://teng-lin.github.io/notebooklm-py/diagrams/22-testing-and-guardrails.html)
links its ownership pins to concrete source and test locations at the same
revision.
