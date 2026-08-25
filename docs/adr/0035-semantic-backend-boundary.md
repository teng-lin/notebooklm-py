# ADR-0035: Semantic backend boundary

## Status

Accepted.

The approved P0-through-P9 sequence is complete, with P7 run after P6 and the P8 cookie-provider
extraction run after the runtime interface froze. P0's catalog and compatibility evidence remain
frozen. P9's row-only web backend has 80 directly executable binding rows and eleven
service-owned workflows, all covered by the P4 policy/deadline/error ledger. P8 composes, rather
than replaces, the accepted authentication owners through an immutable generation and a narrow
provider port. P9 applies principle 2's composite-ownership clause per composite and extends
`Operation` with named primitives rather than narrowing any member. Public-API work and a mobile
backend remain separate decisions.

Programme P10 (semantic-boundary remediation) executes under the amendments recorded in
[Addenda (P10, 2026-08-25)](#addenda-p10-2026-08-25); the status of this record stays `Accepted`.

## Context

The application layer established by ADR-0021 is neutral across CLI, MCP, and REST, but the
library below it is still web-specific. Feature APIs select `RPCMethod`, construct positional
arrays, and decode web rows into public models. Consequently, protocol grammar, retry policy,
resource construction, and feature behavior share one dependency direction.

This code also carries compatibility evidence that a replacement must not discard: the public
Python surface, adapter JSON shapes, exception lattice, metrics and telemetry, RPC overrides,
profile formats, strict row decoders, idempotency classifications, and recorded web fixtures.
The existing runtime composition and middleware chain protect those behaviors, so simplifying
that graph before semantic callers have moved would combine two independent migrations.

The governing implementation plan is
[`2026-08-13-semantic-backend-refactor.md`](../plan/2026-08-13-semantic-backend-refactor.md). Its
P0-through-P9 sequence left the residual boundary defects that programme P10 addresses; the P10
plan is
[`2026-08-25-p10-semantic-remediation.md`](../plan/2026-08-25-p10-semantic-remediation.md).

## Decision

### Boundary and dependency direction

Introduce a private semantic boundary below the existing client and feature facades:

```text
CLI / MCP / REST
        -> _app workflows
        -> NotebookLMClient and feature compatibility facades
        -> semantic services (typed operation input/output)
        -> BackendAdapter
        -> web binding + codec -> existing web runtime and transport
```

- `Operation` is a closed internal key. `OperationDef` binds one concrete input type, output
  type, and semantic `CallPolicy`; it is not a raw-invocation API.
- Semantic services may depend on `BackendAdapter`, neutral records, a clock/deadline, and
  public model projectors. They may not name `RPCMethod`, web arrays, protobuf messages, HTTP,
  cookies, or a backend kind. *(Amended by [addendum D7](#d7--projection-is-a-facade-responsibility-not-a-service-dependency):
  public model projectors are no longer a permitted service dependency.)*
- A backend binding owns protocol dispatch, authentication dependencies, wire encoding and
  decoding, and native-to-neutral error translation. It returns typed neutral records, never
  raw wire values.
- Web bindings resolve method IDs at invocation time through `resolve_rpc_id`; an active
  `NOTEBOOKLM_RPC_OVERRIDES` entry remains authoritative.
- Private projectors construct public models through their normal constructor or
  `dataclasses.replace`, never by bypassing `__init__`, `__setattr__`, `__post_init__`, or
  `__setstate__`.
- Resources remain data. Network activity belongs to a service or backend handler.
- Migrations use add/delegate/delete in one bounded slice, with one execution authority after
  each slice and no runtime old/new fallback.

`NotebookLMClient.rpc_call()` remains the documented web-only escape hatch. It does not become
part of `BackendAdapter` and does not make the semantic operation registry public.

### Compatibility and document graph

This refactor is internal. Existing public imports, signatures, return and exception types,
model mutability, aliases, pickle behavior, adapter payloads, telemetry, logger names, profile
data, and cassettes remain stable. Public `from_api_response()` and `from_row()` classmethods
remain importable; migrated production decoding merely stops calling them. Removal requires the
ADR-0018 runway and is not authorized here.

The exported structured-document graph is an explicit exception to the private-record rule.
`StructuredDocument`, `DocumentBlock`, `DocumentAnnotation`, `TextSpan`, `TableCell`, `ListInfo`,
and their enums are immutable, transport-neutral value types whose invariants define UTF-16
offsets, clipping, rendering, and annotation ordering. A web codec may construct these values
through their public constructors. Positional knowledge remains in the codec/row adapter, and a
guard must prevent the document type module from importing wire/backend modules. Duplicating this
graph as private records is rejected because the projection would restate its invariants and
create a second offset implementation.

The exemption is evidence-backed and fail-closed. The canonical module imports only a closed
standard-library set; `tests/_guardrails/test_document_value_boundary.py` rejects planted backend,
row-adapter, RPC, HTTP, or other runtime dependencies. The same guard exercises a non-default,
nested document through public constructors, UTF-16 slicing, rendering, hashing, and pickle so the
decision does not rest on the empty default sample in the general public-model baseline. The
positional grammar remains independently pinned by 39 `documents` mappings in
`tests/_guardrails/_wire_contract.py`, with no pinned or unmapped document entries.

The three live carriers and their P3 dispositions are:

| Carrier | Current construction and public projection | Independent evidence | P3 disposition |
|---|---|---|---|
| `GET_SOURCE` fulltext | `_source/content.py` passes the body to `_web.codec.documents.decode_structured_document`, which reuses the strict document row adapter and produces `SourceFulltext.document` | `TestSourcesGoldenDecoded::test_get_fulltext_decoded_golden` pins the RPC result while `test_citation_alignment.py` pins the captured tree, UTF-16 offsets, and readable rendering | The first P3 source slice is live; scalar fulltext parsing and the GET_SOURCE authority remain unchanged. |
| Streamed answer `responseDoc.body` | `_row_adapters.chat.AnswerRow.document` delegates to `build_document`, producing `AskResult.answer_document` | Both chat decoded goldens pin the recorded answer document, annotations, extent, anchors, and references; `test_citation_alignment.py` isolates malformed and astral-character cases | `_chat/wire.py` ownership moves with the P6 chat domain, not the P3 decode-path retirement. |
| Streamed citation `fragment.elements` | `_chat/wire.py::extract_text_passages` delegates to `build_blocks`, then projects document ranges into `ChatReference` | `test_ask_with_references_decoded_golden` pins full cited fragments and offsets; `test_citation_alignment.py` pins fragment clipping and answer-anchor joins | Deferred with the P6 chat domain; it shares the same exempt values and coordinate space. |

All other decoded resources follow `wire -> private record -> public model`. P3 is approved on
that basis. It should reuse the proven strict row-adapter logic and wire-contract evidence; it is
not authorization for a cosmetic directory rename or weakened ADR-0011 guard.

### Phase order

P7 runs last as written, after P1-P6 have removed semantic feature callers from `RpcCaller` or
recorded explicit legacy exceptions. Its entry work includes migrating tests away from mutable
chain internals. P7 may then replace the composition holders and generic middleware container,
but must equality-preserve loop affinity, drain/close, retry, auth refresh, errors, metrics,
telemetry, and every public client member and constructor option.

P8 ran after P7. `WebCookieProvider` composes the accepted auth owners from ADR-0016 and ADR-0029
through ADR-0034; it does not absorb profile storage, interactive login, recovery, locking,
persistence, or account routing. `WebCookieGeneration` atomically carries cookies, token pair,
account route, and the generation fence. The provider owns the existing acquisition/refresh kernel
and lifecycle; `WebBackendSession` owns a distinct execution kernel and clones only a newer immutable
value into it. A matching detached backend result is reconciled through the provider before
persistence, without aliasing either mutable jar. The deprecated awaited `from_storage()` path
transfers the constructed provider to the returned client's close lifecycle and does not leak it.
A second backend remains out of scope.

Those constraints are inventory claims about who owns what today, so they are gated by
`tests/_guardrails/test_semantic_p8_provider_boundary_audit.py` (fail-closed post-extraction
provider/import/ownership inventories), `tests/unit/test_semantic_p8_provider_characterization.py`
(generation fencing, ownership, and private-session cloning), and
`tests/unit/test_semantic_p8_auth_adapters.py` (whole storage/refresh transaction delegation).
The existing storage, locking, single-flight, routing, and redaction suites remain unchanged.

### `_app/` orchestration and budgets

ADR-0021 remains in force: `_app/` never imports private siblings and reaches backend work only
through a client/feature facade. The P0 operation catalog records these current orchestrator call
sites and distinguishes their validation, dispatch, and outcome-projection roles from the facade
or exported-helper execution owners:

| `_app/` area | Disposition | Budget route |
| --- | --- | --- |
| `generate.py` / `generate_retry.py` | Keep plan building, ID resolution, optional wait dispatch, progress events, and outcome projection. In P4.2 remove the internal use of the exported `notebooklm.artifacts.with_rate_limit_retry` loop by moving retry execution behind one artifact facade/service entry point; preserve the public helper for external callers. Artifact polling already executes only in the public facade. | The facade accepts scalar timeout/retry inputs and creates or receives one private absolute deadline. `_app` forwards the caller's values once and never imports `RuntimeDeadline`. |
| `source_wait.py` | Keep validation, multi-result shaping, and exception-to-outcome projection. The public source facade remains the sole polling authority. | Forward the existing scalar `timeout` once; the source facade starts the private deadline and threads it through list/poll calls. |
| `download.py` | Keep artifact selection, filename/conflict policy, multi-item composition, progress, and result shaping. Network listing and each download remain separate facade operations. | Each facade operation owns its own semantic deadline from an explicit scalar or client configuration. No artificial command-wide deadline is added to a multi-download workflow. |
| `pagination.py` | Keep the pure bounded slice. Remove its claim about the native protocol; pagination support is a backend capability/catalog fact. | None: it performs no I/O and owns no clock. |

Any new workflow that both composes calls and touches a backend is split by the same rule: the
backend-touching half moves below the facade, while `_app` retains presentation-neutral
composition and passes a scalar budget through a public facade parameter.

### P0 catalog and contract evidence

P0 establishes evidence before runtime delegation:

- `operation_catalog` is the one ADR-0022 baseline for all 86 semantic operations, active RPC
  methods/variants, every public namespace method, every public `NotebookLMClient` member, and
  `_app` orchestrators. Each operation owns exact authority rows with `transport_kind` (`rpc`,
  `stream`, `upload`, `download`, or `orchestrator`), binding, source site, and semantic
  discriminator. Each native binding carries variant-specific decoder/golden evidence with an
  explicit scope/disposition and its own override proof: source dataflow plus a parameterized
  runtime test. Semantic owner, policy, route context, composite behavior, and migration
  disposition remain reviewed metadata. Missing, duplicate, or unallocated authorities and public
  members fail the audit rather than becoming silent omissions. P0 allocates 157 authority rows;
  39 operations have more than one, with 11 reviewed divergences (10 authority and one policy).
  Four native golden rows remain explicitly `not_recorded` rather than claiming
  evidence that does not exist.
- `public_model_contract` freezes every dataclass and enum reachable through the `__all__` of a
  public module discovered by the public API audit, deduplicated by class identity and keyed by
  canonical module plus qualname while retaining every export path. It records constructor and
  field order, dataclass flags, declared slots, equality/hash/repr policy, enum members, and the
  structured outcome of a valid-instance pickle probe: success, mismatch, or failure stage/type.
  First-party pickle-state hook ownership is explicit, and `Notebook` / `ChatReference` exercise
  their legacy-state restore invariants as well as current-state round trips.
- `json_envelope` freezes reviewed, sink/view-backed projections separately for CLI JSON, MCP, and
  REST: projection mode, exact top-level and nested keys, causal public-model fields, and
  reachability evidence. The frozen primary inventory has 31 model identities / 133 projections
  for CLI, 32 / 123 for MCP, and 32 / 57 for REST: 313 unique projection ids. A supplemental
  inventory records full `to_jsonable` keys for every non-secret exported dataclass, but an import
  alone does not make a model channel-reachable.
- The baseline's closed-world `adapter_sink_reachability` section discovers all 350 reviewed
  terminal/error sites: 225 have public-projection dispositions, 117 are reviewed non-public, and
  eight are forwarding infrastructure. It includes CLI JSON success/error/direct emissions, MCP
  tool returns and error funnels plus auxiliary connector/file-route responses, and REST
  route/app/error terminals. Fifteen conditional non-public variants are pinned across 14 mixed
  terminals.
  Every one of the 313 live projection ids has a terminal allocation; new adapter registrations or
  direct JSON bypasses fail closed. The bounded static call graph pins 519 reachable in-package
  nodes / 1,242 edges behind one aggregate digest, alongside 16 explicit helper fingerprints
  (520 unique helpers overall). Thirty-four private DTO -> public dataclass paths are exact: 32 link to live
  projections, while `SourceRefreshResult.result` is proven production-dead and
  `ValidatedSessionConfig.limits` is confined to internal runtime configuration by semantic AST
  fingerprints and mutation tests. Thirty-seven declarations across 28 literal final-dict sites
  derive their top-level shapes from the AST; 168 explicitly reviewed declarations remain manual.
- `AuthTokens` remains excluded from the exported/full-`to_jsonable` inventory and recursive
  credential serialization remains forbidden. It is adapter-reachable only through exactly two
  explicitly marked redacted contributions: MCP `server_info` and REST `server_info`.
  `authuser` / `account_email` may supply emitted account identity; `storage_path` and profile
  session generation may only select cache/fallback control flow. Cookie, token, header, storage,
  and generation values may not become adapter keys, and any third or relocated projection fails
  the derivation.
- `metrics_contract` freezes `ClientMetricsSnapshot` and `RpcTelemetryEvent` ordered field/type
  maps. Its primary scenarios drive public `NotebookLMClient.rpc_call()` through the composed
  middleware and `RpcExecutor`, then read public `metrics_snapshot()` and the event callback for
  success, transport-error, and decode-error outcomes. Direct non-RPC middleware scenarios remain
  supplemental.
- The public API audit covers `collections`; the catalog assigns a semantic/local disposition to
  all 146 namespace methods and an auth/lifecycle/observability/raw disposition to all ten
  public root-client members. An unmatched public member or active RPC/variant is an error.

These baselines are derived/store/compare artifacts under ADR-0022. Architecture PRs may update
their derivation when ownership moves, but may not regenerate a compatibility baseline merely to
accept drift. P0 measurements are taken at this PR's merge base.

### Existing ADR dispositions

| ADR | Disposition |
| --- | --- |
| ADR-0004 | Preserved. Backend and provider objects remain loop-affine with the client lifecycle. |
| ADR-0005 | Preserved as the web retry authority. `CallPolicy` is a derived semantic view, never a second idempotency registry; P4's active-binding ledger audits exact parity and reports divergences without moving enforcement. Amended by [addendum D3](#d3--the-policy-ledger-splits-into-a-derived-half-and-a-reviewed-half-outside-production): the ledger's reviewed half moves out of production and the audit runs as a test, with enforcement still unmoved. |
| ADR-0008 | Preserved. Every code-motion PR retightens a reduced module ceiling. |
| ADR-0009 | Preserved through P6. P7 may supersede the generic chain structure only when equivalent behavioral gates land in the same PR. |
| ADR-0011 | Preserved. P3 moves/extends sanctioned strict-decode homes and the positional-index guard together. |
| ADR-0012 | Preserved. New backend, record, operation, and projector modules remain underscore-private. |
| ADR-0013 | Preserved. The semantic port is a narrow shared capability; backend-specific or single-consumer collaborators remain local. |
| ADR-0014 | Preserved through the transitional backend. P7 may retire `RpcCaller` satisfiers only after their consumers delegate to semantic services; direct leaf-collaborator injection remains the rule. |
| ADR-0016, ADR-0029-0034 | Preserved. P8 composes their identity, logger, storage, recovery, and lifecycle owners behind a provider. |
| ADR-0018 | Preserved. No public decoder, alias, or awaited-factory runway is shortened. |
| ADR-0019 | Preserved. Public error/return contracts and composite partial-availability behavior remain unchanged. |
| ADR-0020 | Preserved in full deferral; P5 does not introduce a sealed generation-result union. |
| ADR-0021 | Preserved and clarified by the `_app/` disposition above. |
| ADR-0022 | Extended by the four registered P0 baselines; its derive/store/compare/regen rules remain authoritative. |
| ADR-0026 | Preserved. P5 retains the manifest-pinned MCP Studio surface and enum bindings. |

ADRs not listed are not changed by this decision.

## Consequences

The wanted result is a protocol-neutral service boundary without a public rewrite: web grammar
terminates in one adapter, model construction points away from wire code, and semantic tests can
use a recording backend. P0 makes compatibility and operation coverage reviewable before code
motion. Resolving the sequence now also gives P3, P7, and P8 concrete entry constraints.

The costs are deliberate. P1-P6 are temporarily net-additive, P3 maintains record/projector
translation for mutable public models, and the existing web runtime remains until P7. The
operation catalog and four baselines add review obligations. P7-last means simplification is
collected late; phase stop/go reviews must prevent temporary bridges from becoming permanent.

## Alternatives considered

**Run P7 first.** Rejected. It would simplify composition while feature APIs still depend directly
on the web RPC vocabulary, combining runtime and semantic migrations and weakening rollback.

**Skip P3 and treat all current row adapters as the final boundary.** Rejected. The adapters were
valuable codec evidence, but the P3 resource-decode retirement now converts their values to
private records before controlled compatibility projection. Retained public factories remain
importable behavioral oracles and have no production decode callers. Only the immutable document
graph is exempt from private-record projection.

**Defer or skip P8.** Rejected. Backend-owned credential acquisition needs an explicit provider
boundary before a backend can have a coherent lifecycle. P8 is sequenced after P7 to avoid moving
auth-refresh ownership twice and must compose, not replace, the accepted auth subsystem.

**Make `_app/` call `BackendAdapter` or import `RuntimeDeadline`.** Rejected. It would reverse
ADR-0021, expose private runtime types above the public facade, and make frontends depend on the
selected protocol.

**Expose operations as a generic public dispatch API.** Rejected. It would freeze an internal
registry, recreate `rpc_call()` with a different enum, and let untyped callers bypass domain
validation and compatibility projection.

## Addenda (P10, 2026-08-25)

Programme P10 is the remediation of the boundary defects P0-through-P9 left behind
([`2026-08-25-p10-semantic-remediation.md`](../plan/2026-08-25-p10-semantic-remediation.md), §1).
Nine governance decisions were taken before any P10 code moves. Seven amend text or a frozen
classification; two confirm an existing ruling and are recorded here so the set is complete. The
sections above are unchanged apart from short pointer notes at the amended locations; where an
addendum amends the governing plan rather than this record, it says so.

"Principle N" below refers to the numbered architectural principles in
[`2026-08-13-semantic-backend-refactor.md`](../plan/2026-08-13-semantic-backend-refactor.md), which
this record's Status section already treats as binding (`P9 applies principle 2's
composite-ownership clause per composite`).

### D1 — Studio generation inputs are resolved above the port

**Decision.** The Studio generation operations (the eight `artifact.generate_*` families,
both `mind_map.generate_*` members, and `notebook.suggest_prompts`) keep their existing
`OperationDef`s. Their input records become **required and pre-resolved**: source-id and language
resolution and option validation happen in the semantic service, and each generation row collapses
to a single-native `CodecBinding`. The "`None` = all sources" convenience stops being a port-level
contract and becomes a **documented service-level default**. Source-scope defaulting is a service
concern.

**What it amends.** Not this record's text: an input-defaulting row that issues its own
`GET_NOTEBOOK` read to fill in a caller's omission performs none of the four things the Decision
assigns to a backend binding (protocol dispatch, authentication dependencies, wire coding,
native-to-neutral error translation). The addendum settles an ambiguity the plan left open, and
settles it **against** the plan's stated preference for per-family `PRIMITIVE` leaves; it also
retires the plan's `deferred-product` custom-row category, whose eleven rows exist only to carry
this defaulting.

**Rationale.** Which sources a product action applies to is a semantic decision, so it belongs
above the port with the rest of input validation. The rejected alternative reaches the same end
state by adding roughly eleven `Operation` members plus their count pins, catalog rows and
capability entries, and would leave the optional-input contract at the port for the lifetime of
those members. A third option — one generic family-keyed kickoff leaf — is rejected outright
because `OperationDef` binds exactly one concrete input type.

### D2 — The value-type exemption is not extended to the chat value types

**Decision.** The exported-value-type exemption in *Compatibility and document graph* stays
scoped exactly to the structured-document graph. It is **not** extended to the frozen
`ConversationTurnKey` / `NextStepSuggestion` types. The chat codec emits the neutral
`ChatTurnKeyRecord` / `ChatNextStepRecord` records that already exist, projected at the facade.

**What it amends.** Nothing. It confirms the exemption's closed scope against a proposal to widen
it, and is recorded because the alternative was live during P10 planning.

**Rationale.** The exemption is evidence-backed and fail-closed on one criterion: the canonical
module imports only a closed standard-library set. The chat value-type module imports `rpc.types`,
so it fails that criterion on its face. The neutral records and their projectors already exist, so
widening the exemption would buy nothing and would convert a guarded, one-off exception into a
precedent that any frozen public type can be constructed in a codec.

### D3 — The policy ledger splits into a derived half and a reviewed half outside production

**Decision.** The **actual** native set per operation is *derived* from the binding rows'
`NativeCallSpec.choices`. The **expected** natives, roles, per-native expected policy, workflow
leaf edges and reviewed `known_divergence` entries stay hand-written and independent of both the
rows and the registry, but move **out of production** into a hand-reviewed intent module under
`scripts/` (beside the existing hand-reviewed catalog-spec metadata) plus a stored ADR-0022
baseline under `tests/fixtures/baselines/`. The production policy module in `_web/` is **deleted**.
`CallPolicy` stays a field on each `OperationDef`. The parity audit compares actual-against-expected
natives and expected-against-registry policy, distinguishes direct-row parity from end-to-end
operation authority, and **runs as a test** rather than at registry construction.

**What it amends.** The ADR-0005 row of *Existing ADR dispositions* — "P4's active-binding ledger
audits exact parity and reports divergences without moving enforcement". The ledger's location and
trigger change; enforcement does not. ADR-0005 remains the web retry authority and `CallPolicy`
remains a derived semantic view, never a second idempotency registry.

**Rationale.** An audit whose two sides are both derived from the rows compares the rows with
themselves and proves nothing, so the reviewed half must stay hand-written. But keeping it in
production duplicates the registry (79 of 80 natives are derivable), hand-lists `RPCMethod` below
the port in a module the boundary is trying to empty, and forces every row change to hand-edit a
1,120-line ledger. Moving the reviewed half to the script side preserves the audit's independence,
removes the duplication from shipped code, and takes an audit out of the import path of registry
construction. `scripts/` never imports `tests/`, so the intent module and the stored baseline stay
on the correct side of that rule.

### D5 — Source registration is no longer classified as protocol-varying

**Decision.** Retire the protocol-variance classification of source registration for
`SOURCE_ADD_URL`, `SOURCE_ADD_URL_BATCH`, `SOURCE_ADD_DRIVE` and `SOURCE_ADD_TEXT`, and authorise a
`SOURCE_REGISTER` primitive leaf so those four workflows are sequenced by a semantic service above
the port. `source.add_file` is unaffected — see D4.

**What it amends.** Principle 2's composite-ownership clause, which names "source
registration/reconciliation" among the examples an adapter handler may own, and the per-composite
application of that clause recorded in this record's Status section and in the P9.2 gate table's
backend-identity column.

**Rationale.** The variance the classification protects is the hypothetical mobile tentative-source
commit; no such backend exists or is authorised. The cost is real and present: four registration
workflows run below the port on **public** models and leak public exceptions through the source
service via `RAW_PASSTHROUGH` rows, which contradicts this record's own rule that a backend binding
"returns typed neutral records, never raw wire values". If a second backend ever needs a different
registration choreography, principle 2's clause can be re-applied to that backend's binding without
restoring public models below this one's port.

### D6 — Capabilities gain an additive `available()` view

**Decision.** `BackendCapabilities.supports(op)` keeps its meaning: *directly invokable through
this backend*. Add `BackendCapabilities.workflows: frozenset[Operation]` and
`available(op) = supports(op) or op in workflows`. The plan's rule-4 deferral of a workflow-aware
capability view is lifted, since P10 supplies the consumer that deferral was waiting for.

**What it amends.** Principle 5 as applied by the plan's ADR-0035 disposition row ("`supports` keeps
meaning invokable; `service_owned` members report `False` and their leaf conjunction is a catalog
row"). The invokability meaning is preserved exactly; the amendment is the additive second view and
the lifted deferral.

**Rationale.** With `supports()` alone the registry cannot distinguish "not a product feature" from
"available, through a service workflow" — the research wait and import-verify operations are
implemented in a service while the registry reports them unsupported. Redefining `supports()` to
mean the union was rejected: `invoke()` gates on it, the recording backend gates its own `invoke` on
it, and the leaf-preflight helper and its call sites all read it, so widening it would make the
backend claim it can directly invoke workflows it refuses. An additive view fixes reporting without
touching enforcement; `workflows` is a data field asserted by tests and reported by the catalog
audit, and no product code branches on it.

### D7 — Projection is a facade responsibility, not a service dependency

**Decision.** Remove "public model projectors" from the permitted semantic-service dependencies
listed in the *Boundary and dependency direction* section. A semantic service returns neutral
records and results, neutral enums, built-in scalars and collections of them, or `None`;
record-to-public-model projection happens at the compatibility facade above it. Named, enumerated
exemptions (the byte-download clients) are carried by a shrink-only guardrail allowlist.

**What it amends.** The Decision bullet "Semantic services may depend on `BackendAdapter`, neutral
records, a clock/deadline, and public model projectors."

**Rationale.** That permission is the mechanism behind the residual defect it was meant to bound:
thirteen service modules import the projector module, public types, or `httpx`, and only two return
records at all. Without this addendum, P10's service-boundary invariant would enforce a rule this
record explicitly permits. Confining projection to the facade is what makes the service layer
neutral in the sense the boundary was introduced for — the record-to-public-model translation layer
itself stays, as a compatibility tax owed to the public contract, and is retired only on an
ADR-0018 runway.

### D9 — `chat.ask` is no longer classified as protocol-varying

**Decision.** `chat.ask` stops being an adapter-owned composite. The product operation becomes
service-owned over a `CHAT_STREAM_ANSWER` primitive carried by a stream-native binding kind, with
the streamed request grammar and answer decoding owned by the chat codec.

**What it amends.** The same principle-2 composite-ownership clause as D5, and the plan's naming of
chat among the plausibly protocol-varying composites, together with the P9.2 gate table's ruling
for that row.

**Rationale.** Identical in shape to D5: the protocol variance is hypothetical, while the cost is a
present leak. Because batchexecute is the only native call kind a row can declare, the chat row is a
custom row that must be handed transport internals through head-injected collaborators — a request
counter, a timeout, and a composed transport — none of which any other row needs. This record
already anticipates the fix in the abstract ("Streaming may use a separate typed method or
context-manager protocol when chat is migrated. Do not force streams through a unary `invoke()`
result"); a stream-native binding kind is the row-level expression of that, and it is what lets the
answer workflow's locks, cache, session hint and follow-up handling sit in a service rather than in
the public facade.

### D4 — `source.add_file` stays adapter-owned (confirmation)

**Decision.** `source.add_file` remains a `protocol` custom row. Its result and failure graph
becomes neutral during P10, but the composite itself is not hoisted.

**What it amends.** Nothing. It confirms that principle 2's composite-ownership clause continues to
apply to this row, and marks it as the deliberate exception to D5 rather than an oversight.

**Rationale.** The signed-URL broker, byte transfer and commit choreography is genuinely
protocol-shaped, so a second backend would not run the same sequence — the exact test the clause
states. Making its records neutral removes the public-model leak without claiming a backend
identity the upload flow does not have.

### D8 — P10 is the programme phase token (confirmation)

**Decision.** This programme's phase token is **P10**. The anonymous-bridge guardrail's current
phase advances from 9 to 10, and every transitional module or branch introduced during P10 carries
a `Removal: P10` marker and is deleted within the programme. There are no one-release shims.

**What it amends.** Nothing; it records the token so the migration rule and the guardrail agree.

**Rationale.** The guardrail dates transitional code against a declared phase, so a programme that
introduces transitional code must own a token. No `Removal: P9` marker survives in the package, so
the bump turns nothing red on the way in and makes every P10 bridge fail closed on the way out.
