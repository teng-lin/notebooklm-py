# ADR-0037: Live usage and quota API

## Status

Accepted — implemented for the public settings API and both Web and Android transports, with
recorded read-only cross-tier coverage.

## Context

NotebookLM exposes two quota systems that must not be conflated:

1. Published per-feature limits, such as notebooks, sources, daily chats, and daily artifact
   generations. `GetOrCreateAccount` exposes only static notebook/source limits. A legacy
   `GetQuota` RPC uses an 18-code feature namespace, but did not report consumption when a feature
   refused work during the investigation in
   [#2283](https://github.com/teng-lin/notebooklm-py/issues/2283).
2. A unified compute meter exposed by `ListQuotaSummary` (`EylDcb`). It uses a separate action-code
   namespace and two percentage windows. The frontend fetches it only when `GetAccount` (`SatQRc`)
   account field `5.7` (`PremiumUserInfo.computeMeteringEnabled`) is true and UI experiment
   `45802040` is enabled.

The SDK cannot observe the frontend's experiment assignment and must not emulate a UI rollout
gate. It treats the server-owned account bit as meter eligibility: when the bit is false it does
not call `ListQuotaSummary`; when true it lets that RPC's status determine data availability.

The initial #2283 cohort had the account bit false, and direct calls returned prices over empty,
sliding windows. An isolated Standard/free profile entered the enabled cohort on 2026-09-04,
allowing bounded submission/completion probes. A separate isolated Pro profile was used only for
read-only cross-tier comparison. The scrubbed observations and evidence boundaries are preserved
in [the usage-meter evidence record](../android/usage-quota-evidence.md).

### Bundle and native-schema evidence

`scripts/capture_rpc_registry.py --save-bundle` fetched a 5,089,854-byte authenticated frontend
bundle on 2026-09-04. Its SHA-256 is
`4069cbdb867f64b3e6a937bf97d38adcb9f0b9b428429895ab04d77265059521`. The registry parser found
all 187 method paths with no fallback or unclaimed path, including:

- `SatQRc` -> `LabsTailwindOrchestrationService.GetAccount`;
- `EylDcb` -> `LabsTailwindOrchestrationService.ListQuotaSummary`;
- `gzAChb` -> `LabsTailwindOrchestrationService.GetQuota`.

The current frontend decoder and live responses give `ListQuotaSummary` this positional shape:

| Location | Meaning | Evidence grade |
| --- | --- | --- |
| response `f1` | status (`1=SUCCESS`, `2=SKIPPED`, `3=FAILED`) | exact recovered enum |
| response `f2` | repeated window rows | current bundle plus live Web/Android |
| response `f3` | repeated `UserAction outOfQuotaActions` | exact Android field declaration plus live correlation |
| response `f4` | repeated action rows | current bundle plus live Web/Android |
| window `f5` | window code (`1` five-hour, `2` weekly in live behavior) | bundle plus live timing |
| window `f6` | reset timestamp | bundle plus live timing |
| window `f7` | `usedMicrosPercent`, elided at zero | exact Android field declaration plus live transition |
| window `f8` | `remainingMicrosPercent`, elided at zero | first-party protobuf JSON plus live gRPC equality |
| action `f1` | exact `UserAction` enum | exact Android field declaration; live `1..22` |
| action `f2` | `hasSufficientQuota` | exact Android field declaration; absent and false both disable in the UI |
| action `f3` | `remainingDeferredArtifactGenerations` | first-party protobuf JSON; observed `3` Standard / `6` Pro |
| action `f4` | exact `ActionCostTier` enum | exact Android field declaration |
| action `f5` | `actionPriority` | first-party protobuf JSON; only `INTERACTIVE` / wire `1` observed |
| action `f6` | `estimatedCostPctOfBudget` | exact Android field declaration plus live cross-tier responses |

The recovered `QuotaSummaryEntry_QuotaRefreshWindow` enum names codes `1` and `2` as `HOUR` and
`WEEK`. Live reset intervals establish that the current first row is five hours rather than one
hour. The first-party endpoint instead identifies action `f5` as `actionPriority`; only
`INTERACTIVE` / wire `1` is observed, so the public model still omits it.

Response `f3` tracked unavailable actions in every observed state: `[3]` when only action `3` was
unavailable, absent on unused Pro when every action was available, and `[1..22]` when the
Standard/free five-hour window was exhausted and every action was unavailable. The APK declaration
identifies it exactly as repeated `UserAction outOfQuotaActions`. The public API already carries
quota sufficiency per action, so it does not expose this redundant aggregate.

The recovered response field is typed by the metering `UserAction` enum, supplying this exact
protocol map:

| Code | Recovered name | Code | Recovered name |
| ---: | --- | ---: | --- |
| 1 | `AUDIO_OVERVIEW` | 12 | `CANVAS` |
| 2 | `VIDEO_OVERVIEW` | 13 | `SLIDES_EDITING` |
| 3 | `BREAKDOWNS_VIDEO` | 14 | `FLASHCARD_EDITING` |
| 4 | `SHORTS_VIDEO` | 15 | `DEEP_RESEARCH` |
| 5 | `INFOGRAPHIC` | 16 | `NOS` |
| 6 | `SLIDES` | 17 | `FAST_RESEARCH` |
| 7 | `REPORTS` | 18 | `QNA` |
| 8 | `TABLES` | 19 | `NOS_IMAGE_GENERATION` |
| 9 | `FLASHCARDS` | 20 | `GUIDED_VIEW` |
| 10 | `QUIZ` | 21 | `DOCUMENT_GUIDE` |
| 11 | `MINDMAP` | 22 | `SUGGESTION_CHIPS` (newer server value) |

The current Web quota-action translator maps the repository's present artifact-type labels onto
apparently conflicting action codes: Mind Map to `5` and Infographic to `8`, while the exact
metering enum names those codes `INFOGRAPHIC` and `TABLES`. That inconsistency belongs to the Web
translation call site or the repository's artifact-type interpretation; it no longer creates
uncertainty about the quota response's declared enum type. The first-party protobuf JSON endpoint
names newer code `22` `SUGGESTION_CHIPS`. The public API therefore exposes exact names `1..22` and
keeps future numeric codes observable with `kind=None`.

The frontend selects and displays windows as follows:

```python
selected = weekly if weekly.used_percent >= 100 else five_hour
displayed_used = 100 if selected is exhausted_weekly else round(100 - selected.remaining_percent, 2)
```

It schedules a refresh for the selected reset timestamp plus two seconds. Deep Research looks up
action code `15` and disables the operation when quota sufficiency is false. Window rows arrive in
nondeterministic order.

### Live accounting observations

The bounded Standard/free probe used one explicit ready source in an owned notebook. Its only
writes were one Flashcards generation and one Quiz generation. Both completed and were deleted;
no scratch artifact remains.

The compute meter exposes percentages, not a balance or capacity. Current prices become integers
when a Standard percentage is multiplied by `120`, establishing a *minimal normalized five-hour
price denominator* of 12,000 units. Pro prices observed read-only on 2026-09-04 were exactly one
quarter of Standard prices, giving a minimal 48,000-unit Pro price denominator. These are useful
credit-like arithmetic inferences, not proof of a provider-named or indivisible "credit" unit and
not public API fields. Separately, action field `f3` is now known to be a literal remaining count of
deferred artifact generations (`3` Standard/free, `6` Pro where emitted); it is not the percentage
meter's hidden unit.

| Event | Five-hour used | Weekly used | Normalized observation |
| --- | ---: | ---: | --- |
| before first generation | `0%` | `0%` | timestamps still slid with wall clock |
| Flashcards submitted | `1.8666666667%` | `0.0888888889%` | exact 224-unit advertised reservation |
| Flashcards completed | `0.8685427667%` | `0.0413591794%` | settled to 104.225132 normalized units |
| Quiz submitted | `3.1685427667%` | `0.1508829889%` | added exact `2.3%` / 276-unit reservation |
| Quiz completed | `1.7261085%` | `0.0821956429%` | Quiz settled to 102.907888 normalized units |
| both artifacts deleted | unchanged | unchanged | these deletions did not refund usage |

For these two action families, submission reserved the advertised percentage and completion
reconciled it to a lower fractional debit. The settlement appears likely to depend on generated
work, but two different actions do not establish a workload formula. Repeated same-action probes
with controlled input and output sizes would be required before making that claim.

The weekly percentage represented the same debit against an observed denominator exactly 21 times
the five-hour denominator: `1.7261085 / 21 == 0.08219564285714286`. Both reset timestamps anchored
when the first reservation was created; the second reservation did not move them. This does not
distinguish a fixed bucket from every possible rolling-expiry implementation, so callers must use
the server timestamp rather than recreate reset logic.

The legacy `GetQuota` response remained unchanged across these debits. Most rows carried signed
int64 max (`9223372036854775807`) and rows 4 and 5 carried `75` and `100`; their meanings remain
unknown. It must not be joined to the metering action codes.

Read-only native Android calls reproduced the Standard settled percentages and reset timestamps
exactly. Read-only Pro calls returned identical Web/native action rows and prices, with empty
sliding windows. No Pro reservation, settlement, or capacity was probed.

## Decision

Add the live meter to the existing account-scoped settings namespace:

```python
usage = await client.settings.get_usage()
```

`SettingsAPI` already owns `get_account_limits()` and account RPCs. A new root namespace would add
assembly, backend-manifest, fake, and lifecycle surface without a distinct state owner. The
CLI exposes this API through `notebooklm usage --json`.

### Public model

The backend-neutral model is:

```python
class UsageSummaryStatus(str, Enum):
    DISABLED = "disabled"  # account compute-meter bit is false; summary RPC not called
    READY = "ready"  # ListQuotaSummary status SUCCESS
    SKIPPED = "skipped"  # ListQuotaSummary status SKIPPED


class UsageWindowKind(int, Enum):
    FIVE_HOUR = 1
    WEEKLY = 2


class UsageActionKind(int, Enum):
    AUDIO_OVERVIEW = 1
    VIDEO_OVERVIEW = 2
    BREAKDOWNS_VIDEO = 3
    SHORTS_VIDEO = 4
    INFOGRAPHIC = 5
    SLIDES = 6
    REPORTS = 7
    TABLES = 8
    FLASHCARDS = 9
    QUIZ = 10
    MINDMAP = 11
    CANVAS = 12
    SLIDES_EDITING = 13
    FLASHCARD_EDITING = 14
    DEEP_RESEARCH = 15
    NOS = 16
    FAST_RESEARCH = 17
    QNA = 18
    NOS_IMAGE_GENERATION = 19
    GUIDED_VIEW = 20
    DOCUMENT_GUIDE = 21
    SUGGESTION_CHIPS = 22


class UsageActionCostTier(int, Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    VERY_HIGH = 4


@dataclass(frozen=True)
class UsageWindow:
    kind: UsageWindowKind
    used_percent: float
    remaining_percent: float
    resets_at: datetime


@dataclass(frozen=True)
class UsageAction:
    code: int
    kind: UsageActionKind | None
    has_sufficient_quota: bool
    cost_tier: UsageActionCostTier | None
    remaining_deferred_artifact_generations: int | None
    estimated_cost_percent: float | None


@dataclass(frozen=True)
class UsageSummary:
    status: UsageSummaryStatus
    windows: tuple[UsageWindow, ...] = ()
    actions: tuple[UsageAction, ...] = ()
```

These types are public from both `notebooklm.types` and the top-level `notebooklm` facade, following
ADR-0017. `UsageSummary` provides these exact conveniences:

- `enabled: bool` is false only for `DISABLED`; it remains true for a server `SKIPPED` result.
- `available: bool` is true only for `READY`.
- `window(kind: UsageWindowKind) -> UsageWindow | None` and
  `action(code: int | UsageActionKind) -> UsageAction | None` perform lookups.
- `active_window: UsageWindow | None` returns `None` unless status is `READY`; otherwise it returns
  weekly when weekly usage is at least `100.0`, and five-hour in every other case.
- `is_exhausted: bool | None` returns `None` unless status is `READY`, otherwise
  `active_window.used_percent >= 100.0`.

### Read and decode contract

`get_usage()` follows these rules:

1. Call side-effect-free `GetAccount`. If `computeMeteringEnabled` is false or absent, return
   `UsageSummary(status=DISABLED)` without calling `ListQuotaSummary`.
2. Otherwise call `ListQuotaSummary` live and uncached. On `SUCCESS`, decode a `READY` summary. On
   `SKIPPED`, return `UsageSummary(status=SKIPPED)` with empty windows/actions. On `FAILED`, raise
   `ServerError`. Missing, zero, or unknown status is schema drift and raises `DecodingError` with
   the method identity.
3. A successful response must contain exactly one five-hour row and one weekly row. Missing,
   duplicate, or unknown window codes are schema drift. Rows and actions are sorted by numeric code.
4. Non-finite window percentages are schema drift and raise `DecodingError`. Preserve both finite
   server floats when used and remaining are present, even if temporarily
   non-complementary. When exactly one is elided by protobuf-default encoding, derive the missing
   value as `100.0 - present_value`; a live unused row omitted used `0`, while an exhausted row
   omitted remaining `0`. Both absent is schema drift. A valid protobuf timestamp and window code
   are required. Do not clamp valid values. `resets_at` is timezone-aware UTC.
5. Map exact `UserAction` codes `1..22` to `UsageActionKind`; retain future codes with `kind=None`.
   Missing `hasSufficientQuota` normalizes to `False`, matching frontend behavior. Map exact
   `ActionCostTier` codes `1..4`; absent/zero or future values become `None` without dropping the
   action. Preserve a present non-negative `remainingDeferredArtifactGenerations`; absence becomes
   `None`, and a negative value is schema drift. Missing advertised cost remains `None`; a
   non-finite advertised cost is schema drift.
6. Do not expose `actionPriority` yet: only `INTERACTIVE` / wire `1` is observed and its complete
   enum declaration is unavailable. Also omit the redundant `outOfQuotaActions` list at response
   `f3`. Raw APIs remain available for investigation.
7. Treat `estimated_cost_percent` as a current server estimate. Never subtract it locally as a
   final debit or enforce a client-side quota from it.
8. Do not expose normalized units, capacity, or a client-computed reset. Percentages and timestamps
   are the server's authoritative contract.

### Transport contract

Add `RPCMethod.GET_ACCOUNT = "SatQRc"` and
`RPCMethod.LIST_QUOTA_SUMMARY = "EylDcb"`, both classified as replay-safe reads.

Web requests are distinct:

- `GetAccount` serializes the empty request `[]`.
- `ListQuotaSummary` serializes `[RequestContext]` and decodes response fields `f1`/`f2`/`f4`.

Android uses unary calls on
`/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/{Method}`:

- `GetAccount`: empty request bytes, modeled locally by the wire-equivalent
  `google.protobuf.Empty`; the exact remote request FQN is not recovered. Its response is an
  envelope containing `google.internal.labs.tailwind.orchestration.v1.Account` at field `1`. The
  envelope parses without top-level unknown fields as the existing `GetOrCreateAccountResponse`
  overlay and returns the same semantic account field set as a live `GetOrCreateAccount` call; its
  exact remote response FQN is nevertheless not recovered. Extend the nested exact-package
  `PremiumUserInfo` subset with optional `compute_metering_enabled = 7`.
- `ListQuotaSummary`: exact recovered request
  `google.internal.labs.tailwind.api.v1.ListQuotaSummaryRequest` with `request_context = 1` and exact
  recovered response `google.internal.labs.tailwind.metering.v1.ListQuotaSummaryResponse`. Its
  stable APK field subset includes response `f1..f4`, action `f1`/`f2`/`f4`/`f6`, and window
  `f5..f7`. The first-party protobuf JSON endpoint supplies the live-only names
  `remainingMicrosPercent #8`, `remainingDeferredArtifactGenerations #3`, and
  `actionPriority #5`; they require a local presence-aware overlay because their newer protobuf
  declarations are absent from the APK.

The official Android app does not invoke quota through that gRPC service. Its generated protobuf
RPC client binds
`ListQuotaSummaryRequest -> ListQuotaSummaryResponse` to service
`google.internal.labs.tailwind.api.v1.QuotaService`, method `ListQuotaSummary`, HTTP annotation
`GET v1/quota:listQuotaSummary`, and JSPB-lite payloads. A read-only live call to the corresponding
gRPC path `/google.internal.labs.tailwind.api.v1.QuotaService/ListQuotaSummary` returned
`UNIMPLEMENTED`, while the orchestration gRPC path accepted the same 52-byte context request and
returned the quota response. Both gRPC reflection v1 and v1alpha are also `UNIMPLEMENTED`.
In one same-session Pro comparison, first-party HTTP JSON and the orchestration gRPC response
matched for status, both windows, and all 22 action rows; the complete local overlay left zero
unknown fields at the response, window, and action levels.

Consequently, the APK proves the exact quota message identities and field declarations but not a
generated binding for the orchestration alias. Record the alias in
`grpc-service-signature-inferences.json` with the cross-service binding and live-response evidence;
do not add it to the APK-extracted signature CSV.

The Android implementation must not pretend either the unrecovered `GetAccount` identities or the
live-only quota fields form exact generated service evidence:

- `ListQuotaSummary` has exact message identities and a live-validated inferred orchestration
  signature. Its response overlay is recorded in `grpc-runtime-parser-overrides.json`, whose
  `remote_type` matches the inferred generated descriptor signature.
- `GetAccount` cannot use that registry because both remote message FQNs are unknown. Upgrade
  `grpc-service-signature-exceptions.json` to schema version 2 and add one path-only entry containing
  the full path, local empty-request codec, local envelope-response codec,
  `reason_code="remote_fqn_unrecovered"`, and this evidence record. Do not invent descriptor types
  or add this method to the generated service.

The Android service-manifest guardrail changes from "every adapter method is in the descriptor" to
"every adapter method is in exactly one of the descriptor or the path-only exception manifest."
Exception entries require allowlisted reason codes, importable codecs, an evidence link, an explicit
retry classification, and exact synthetic request/response byte tests. Runtime-parser overrides
continue to require a descriptor and are forbidden for path-only exceptions.

`proto-evidence-ledger.md` must admit the account bit, recovered identities, wire-equivalent empty
request and response envelope, tags, cardinality, and live evidence before code merges. Both
methods remain replay-safe reads in the Android retry manifest and never fall back to Web.

### Errors and implementation boundary

Typed quota-refusal detection is outside this decision. Existing generation paths continue to
raise `RateLimitError`; current Web and Android error payloads do not yet support a safe,
transport-neutral split. A future `QuotaExceededError` requires its own evidence and a subtype-first
decision for retry helpers, `_app` classification, CLI, MCP, REST, HTTP status, and Android status
details. `get_usage()` is never called implicitly while decoding another operation's exception.

The first implementation includes:

- the Python method, public types, both adapters, public facade exports, `docs/python-api.md`, and
  exact abstract/backend contract updates;
- Web RPC idempotency entries, wire-constant registration, golden decoded coverage, and separate
  read-only Standard/free and Pro VCR cassettes recorded under explicit profiles;
- Android evidence-ledger admission, deterministic proto regeneration where exact fields exist,
  a quota-response runtime-parser override, an inferred `ListQuotaSummary` orchestration signature,
  the narrow `GetAccount` path-only signature exception, service/retry guardrail updates, exact
  synthetic protobuf fixtures, and authenticated read-only parity coverage;
- unit matrices for every response status, either-side percentage elision, both percentages absent,
  non-complementary floats, missing/duplicate/unknown windows, future action `23`, missing
  availability, absent/negative deferred-generation counts, absent/future cost tiers, ordering,
  non-finite values, UTC timestamps, and disabled-call count.

The Android cassette redactor replaces numeric values, so exact percentage parity belongs in
synthetic protobuf fixtures rather than an ordinary cassette. Cross-tier recordings run as separate
processes with `NOTEBOOKLM_PROFILE=YOUR_STANDARD_PROFILE` and
`NOTEBOOKLM_PROFILE=YOUR_PRO_PROFILE`; an operation never switches identity mid-run. Ordinary CI
performs no generation and consumes no metered usage.

The top-level `notebooklm usage` command projects this API in the `SectionedGroup` Session section.
It shows both windows, optionally expands usage categories with `--categories` (alias `--actions`), and emits the full
snapshot with `--json`, including explicit ISO 8601 reset timestamps. The CLI contract baseline,
documentation, and JSON timestamp assertions cover this projection. MCP and REST usage projections
remain additive follow-ups.

## Consequences

- Callers receive authoritative live percentages, availability, and reset timestamps without a
  guessed balance or hardcoded price table.
- Reservation spikes can fall after settlement; usage is a snapshot, not a monotonic counter.
- Static plan limits and live compute usage remain distinct but discoverable in one namespace.
- Exact recovered action names `1..22` are exposed; future codes remain observable without a
  guessed name.
- Disabled, skipped, failed, and malformed results have different explicit behavior.
- Both transports implement the same values without compatibility fallback, with Android's exact
  stable quota subset, live-only overlay, and inferred orchestration alias recorded separately.
- Typed quota-refusal errors remain deferred rather than silently retaining retry semantics.

## Alternatives considered

**Create `client.usage` as a new root namespace.** Rejected initially. Usage is account-scoped and
settings already owns account limits. A root namespace can be reconsidered if history, budgeting,
or mutations create an independent domain.

**Expose credits or normalized compute units.** Rejected. The server sends percentages. The 12,000
Standard/free and 48,000 Pro denominators are arithmetic inferences, not unique ledger capacities
or provider-named units. The separately named `remainingDeferredArtifactGenerations` field is a
per-action fallback count, not the percentage ledger's denomination.

**Use the Web artifact enum for action names.** Rejected. The quota response field is declared as
the recovered metering `UserAction` enum, so those names are authoritative for this API even though
the current Web translator exposes an adjacent mapping inconsistency. The first-party endpoint
closes newer value `22` as `SUGGESTION_CHIPS`.

**Call the APK's `QuotaService` path over gRPC.** Rejected. The app uses its annotated protobuf HTTP
client for that service, and the same full path returns `UNIMPLEMENTED` on the NotebookLM gRPC
endpoint. The live orchestration alias is the native transport route.

**Defer native `GetAccount` until both remote FQNs are recovered.** Rejected for the initial
backend-neutral implementation. The empty request and response envelope are live-proven byte
contracts. A single evidence-linked, wire-equivalent path exception is narrower and auditable; it
must be removed when exact identities are recovered.

**Expose `actionPriority`.** Deferred. The first-party endpoint names the field and maps wire `1` to
`INTERACTIVE`, but no other value or complete enum identity is recovered and the frontend does not
consume it.

**Return only the frontend-selected window.** Rejected. Both server windows are authoritative;
`active_window` supplies UI-compatible convenience without discarding data.

**Use `GetQuota` as the API.** Rejected. Its 18-code namespace is distinct, finite values are
unexplained, and it did not change when the unified meter charged two actions.

**Add `QuotaExceededError` in the same change.** Rejected. Subclassing `RateLimitError` without
changing every subtype-sensitive classifier and retry surface would preserve the misleading
behavior. Native error details also need independent safe-decoding evidence.
