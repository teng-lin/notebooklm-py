# ADR-0035: Semantic backend boundary

## Status

Accepted.

The approved P0-through-P8 sequence is complete, with P7 run after P6 and the P8 cookie-provider
extraction run after the runtime interface froze. P0's catalog and compatibility evidence remain
frozen; all 82 active semantic handlers now share the P4 policy/deadline/error ledger. P8 composes,
rather than replaces, the accepted authentication owners through an immutable generation and a
narrow provider port. Public-API work and a mobile backend remain separate decisions. A proposed P9
(web-backend decomposition into transport, codec, and binding table) is described in the plan; if
approved, it applies principle 2's composite-ownership clause per composite and extends `Operation`
with named primitives rather than narrowing any member.

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
[`2026-08-13-semantic-backend-refactor.md`](../plan/2026-08-13-semantic-backend-refactor.md).

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
  cookies, or a backend kind.
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
| ADR-0005 | Preserved as the web retry authority. `CallPolicy` is a derived semantic view, never a second idempotency registry; P4's active-binding ledger audits exact parity and reports divergences without moving enforcement. |
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
