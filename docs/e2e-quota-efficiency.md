# E2E quota investigation and optimizations

## Incident: September 11, 2026

[Failed run](https://github.com/teng-lin/notebooklm-py/actions/runs/34568547259)
and [successful run](https://github.com/teng-lin/notebooklm-py/actions/runs/34443781982)
both checked out `9adf110d96ea519475e7f505c5a9edd0f91526ce` for the candidate and
trusted CI helpers. Installed dependencies, Python, runner image and action
revisions matched. The installer changed from uv 0.12.12 to 0.12.13.

The Windows lane failed during `prepare_reference`'s conversation-seeding ask,
with `ERROR[QUOTA]: lifecycle provision failed: RateLimitError`. Tests never
started. Its account B had just run the Ubuntu suite, which recorded 20
rate-limit skips, including seven chat skips. On September 10, Windows used
account A **before** that account's Ubuntu suite. The account concurrency group
serializes jobs but does not specify their order.

## Implemented changes

### Run Windows before the full suites

The planner partitions the existing lane selection into read-only and full
matrices. `e2e-readonly` runs first; the full Web and Android jobs depend on its
completion and can then run concurrently on distinct accounts. Both jobs retain
the account-wide concurrency group, protected environment, pinned target and
trusted credential-materialization steps. A shared YAML step sequence keeps
provisioning, test execution, floors, cleanup and aggregation identical.

A filtered `all` dispatch still omits Windows, an explicit `readonly` dispatch
still applies its read-only marker filter, and Web/Android-only dispatches do not
run Windows. Full suites still execute after a Windows failure so their results
remain available; the Windows failure continues to fail the workflow. Cancelling
the workflow prevents the dependent full suites from starting.

This fixes within-run starvation of Windows provisioning. It does not reset an
account's quota or guarantee capacity after unrelated account use. No quota
failure is converted into success, and no ambiguous chat request is retried.

The secret-gate scanner now expands block step aliases in each consuming job's
own context and allows only enumerated scheduling restrictions alongside both
mandatory trust predicates. Negative tests cover weakened alias-consumer gates,
environment, concurrency and token selection.

### Reuse live response values, preserving behavioral checks

| Area | Before | After | Saving on an unthrottled run |
| --- | --- | --- | --- |
| Six chat answer/citation tests | Six asks and six fresh-conversation resets | One ask and one reset, shared as copied response values | Five asks and up to ten reset API calls per full backend |
| Source-selection module, default selection | Nine source-list calls | One source-list call | Eight reads per full backend |
| Source-selection module, variants included | Twelve source-list calls | One source-list call | Eleven reads per full backend |
| Prepared reference validation | Preparation readback followed immediately by a duplicate validation readback | Preparation's complete readback validates the reference | Five public read calls per provisioned reference |

These are public API invocation counts, not measured wire-RPC totals or claims
about Google's quota accounting. An API method can make several wire requests.
The two full nightly backends together save ten chat submissions and sixteen
source-list invocations on the default path. Subsets, retries, early failures
and throttling change the realized savings.

All 237 E2E test nodes remain. In the two optimized test modules, all 28 original
test nodes and their original assertions remain. The six shared-answer tests
still check answer shape, conversation metadata, citation IDs, cited text,
citation numbering and membership in the notebook's sources. Two additional
assertions require real references and cited text, so a citation-free answer
cannot make those checks pass without exercising the decoder.

Samples are local to a test module and keyed by notebook, backend and retry
attempt. Only successful results are retained, and each consumer gets a deep
copy. Clients remain function-scoped; no event loop, connection or async client
is shared across tests. Failed operations are never cached as successful samples.

Distinct live request semantics remain separate: omitted versus explicit source
selection, single sources, subsets, reordered sources, follow-ups, deletion and
fresh conversations. Source CRUD modules retain fresh reads. Generation method,
option and adapter coverage is unchanged; all generation journals and coverage
floors remain active.

## Further opportunities from the audit

| Opportunity | Coverage constraint | Next step |
| --- | --- | --- |
| Repeated artifact discovery in downloads/MCP helpers | Copied artifacts arrive asynchronously; payload URLs can be absent or expire | Reuse settled candidate IDs only, with fresh download/Get calls and refresh-on-miss. Do not cache an early empty inventory. |
| Repeated chat-history response checks | The suite exercises both limit 2 and limit 20 plus distinct history/turn APIs | Share response snapshots only among identical requests; keep both limits and every API entrypoint live. |
| Artifact generation reused for rename/delete/readback | Cleanup, source selection, generation options and each adapter path are separate contracts | Extend lifecycle reuse only where the same generated object can satisfy all assertions without losing an entrypoint or request variant. The existing poll/rename/wait test already does this. |
| Polling volume | Reducing polling must not reduce deadlines or skip terminal-state assertions | Record per-method request counts and observed completion times before changing intervals. Retain final completion verification. |
| Additional account isolation | One or two enabled accounts necessarily share quota among lanes | A third enabled account separates the three nightly lanes. Scheduling still protects a small account pool. |

Do not treat fewer requests as equivalent to a known daily quota reduction:
chat submissions, Studio generation, research and ordinary reads can hit
different upstream limits. Avoid blanket method caches, broader skip rules,
removing live adapter tests, or reducing the nightly selection to obtain a green
run.

## Validation

339 focused offline tests passed. They cover sample call counts, notebook/backend/retry isolation,
consumer mutation, error propagation, citation coverage, provisioning readback,
all lane/filter/account-pool combinations, alias-consumer secret gates, journals
and execution-floor helpers. Collection still finds 237 E2E nodes. Ruff and the
workflow secret, permission and action-pinning checks passed. No live NotebookLM
requests were made for validation.

Actionlint 1.7.12 supports the YAML aliases but does not recognize the existing
`concurrency.queue: max` field. Validate with that specific diagnostic ignored;
retain the repository's explicit queue checks. Live qualification remains a
separate validation step; the offline checks spend no NotebookLM quota.
