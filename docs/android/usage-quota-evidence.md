# Live usage-meter evidence

**Status:** bounded live observations supporting the admitted ADR-0037 implementation

**Snapshot date:** 2026-09-04

**Scope:** `GetAccount`, `ListQuotaSummary`, and comparison with legacy `GetQuota` on one
Standard/free profile and one Pro profile

This record preserves the scrubbed facts used by ADR-0037 without credentials, cookies, account
identifiers, notebook identifiers, source identifiers, or generated content. The authenticated
frontend bundle is not checked in: it is 5,089,854 bytes, volatile, and provider-owned. Its SHA-256
identity and the reproducible registry-capture command are retained instead.

## Inputs and commands

| Input | Identity or command | Role |
| --- | --- | --- |
| current authenticated Web bundle | SHA-256 `4069cbdb867f64b3e6a937bf97d38adcb9f0b9b428429895ab04d77265059521` | constructors, translators, frontend selection behavior |
| bundle capture | `NOTEBOOKLM_PROFILE=YOUR_STANDARD_PROFILE uv run python scripts/capture_rpc_registry.py --save-bundle /tmp/notebooklm-standard-profile-2026-09-04.js --json` | reproducible current-bundle acquisition with a local profile placeholder |
| signed Android `1.55.10.971450265` AOT pool | `pp.txt` SHA-256 `c2b64fd7d08a64f833b343f54bc697520096dfaef10740ebcbcd66a5c8e24b9a` | exact quota RPC generic binding, route annotation, and message fields |
| signed Android AOT symbol map | `ida_script/addNames.py` SHA-256 `b75adf9f8bb92085c853dd30d231d533aec987024cf0e442f7cafc03dab24518` | exact Dart library/package identity |
| recovered Android schema | [`schema.proto`](schema.proto), evidence identity in [`proto-evidence-ledger.md`](proto-evidence-ledger.md#evidence-input-identities) | quota messages, request FQN, and account bit |
| recovered Android enums | [`enums.txt`](enums.txt), evidence identity in [`proto-evidence-ledger.md`](proto-evidence-ledger.md#evidence-input-identities) | status, action, cost-tier, and refresh-window enums |
| prior investigation | [issue #2283](https://github.com/teng-lin/notebooklm-py/issues/2283) | initial disabled cohort and legacy namespace comparison |

The registry capture parsed 187 of 187 method paths with no fallback or unclaimed path. Relevant
registrations were `SatQRc=GetAccount`, `EylDcb=ListQuotaSummary`, `gzAChb=GetQuota`, and
`sODAg=GetArtifactDeferredGenerationQuota`.

The current Web quota-action translator preserves this exact numeric mapping:

```text
ArtifactTypeCode 1                         -> action 1
ArtifactTypeCode 3, ordinary video         -> action 2
ArtifactTypeCode 3, video format 3          -> action 3
ArtifactTypeCode 3, video format 4          -> action 4
ArtifactTypeCode 5                         -> action 5
ArtifactTypeCode 6                         -> action 6
ArtifactTypeCode 2                         -> action 7
ArtifactTypeCode 7                         -> action 8
ArtifactTypeCode 4, variant 1/2/4/5        -> action 9/10/11/12
```

The repository identifies artifact types 5 and 7 as Mind Map and Infographic, while the mobile
`UserAction` enum names actions 5 and 8 as `INFOGRAPHIC` and `TABLES`. The APK now also proves that
quota action field `1` and `outOfQuotaActions` are typed by that exact enum. The conflict therefore
belongs to the adjacent Web translator or the repository's artifact-type interpretation, not to
the response field identity. Live generation independently corroborates action 9 as Flashcards and
action 10 as Quiz; current frontend behavior corroborates action 15 as Deep Research. The live
first-party protobuf JSON response closes newer code 22 as `SUGGESTION_CHIPS`.

### CLI display names

Protocol enum names are not necessarily the names shown in the product. The recorded translator
maps video format `3` to action `3`; `VideoFormat.CINEMATIC = 3` and the cinematic generation builder
establish its CLI label as **Cinematic video**, despite the protocol name `BREAKDOWNS_VIDEO`.
Other display labels expand abbreviations or match existing CLI features: `QNA` becomes **Chat
Q&A**, `SUGGESTION_CHIPS` becomes **Suggested questions**, and `DOCUMENT_GUIDE` becomes **Source
guide** (the source snippet/main-ideas response from `GenerateDocumentGuides`).

`NOS` and `NOS_IMAGE_GENERATION` remain exact recovered protocol names. The adjacent Android
`ClientCapability.NOTEBOOK_OS_AGENCY` and `SuggestionConfigId.SUGGESTION_CONFIG_NOS_APP` names suggest
an internal Notebook OS connection, but do not establish the quota actions' public feature mapping.
The CLI therefore retains `NOS` with an explicit uncertainty note. JSON `kind` preserves all exact
enum names; these presentation labels do not rename protocol values.

## Android static and route recovery

The official app contains an exact generated protobuf RPC binding with these properties:

```text
request:   google.internal.labs.tailwind.api.v1.ListQuotaSummaryRequest
response:  google.internal.labs.tailwind.metering.v1.ListQuotaSummaryResponse
service:   google.internal.labs.tailwind.api.v1.QuotaService
method:    ListQuotaSummary
HTTP:      GET v1/quota:listQuotaSummary
payload:   jspblite2
```

This is the app's annotated protobuf HTTP client, not one of its extracted gRPC client bindings.
The request declares `RequestContext requestContext = 1`. The response's stable APK schema is:

```text
ListQuotaSummaryResponse:
  #1 Status status
  #2 repeated QuotaSummaryEntry summaries
  #3 repeated UserAction outOfQuotaActions
  #4 repeated UserActionQuotaSummary actionQuotaSummaries

QuotaSummaryEntry:
  #5 QuotaRefreshWindow window
  #6 google.protobuf.Timestamp nextRefreshTime
  #7 double usedMicrosPercent

UserActionQuotaSummary:
  #1 UserAction action
  #2 bool hasSufficientQuota
  #4 ActionCostTier costTier
  #6 double estimatedCostPctOfBudget
```

Tags `1..4` of `QuotaSummaryEntry` and tags `3`/`5` of `UserActionQuotaSummary` are explicitly unused
in this APK. The live first-party protobuf JSON response closes the newer fields as
`remainingMicrosPercent #8`, `remainingDeferredArtifactGenerations #3`, and `actionPriority #5`.
The only observed action-priority label is `INTERACTIVE`, equal to native wire value `1`.
`remainingDeferredArtifactGenerations` was `3` on Standard/free and `6` on Pro for the action
families where the field was emitted. It is a separate remaining-count field, not evidence that the
percentage ledger's normalized arithmetic units are provider credits.

The official HTTP route is
`https://labstailwind.pa.googleapis.com/v1/quota:listQuotaSummary`; a bearer-authenticated read-only
GET returned protobuf JSON. Read-only native route probes used
`NOTEBOOKLM_PROFILE=YOUR_PRO_PROFILE` and
the exact 52-byte context request. The corresponding gRPC path
`/google.internal.labs.tailwind.api.v1.QuotaService/ListQuotaSummary` returned `UNIMPLEMENTED`. The
mobile-backend alias
`/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/ListQuotaSummary`
returned a 518-byte response matching the recovered message and earlier Web/native values. A
same-session HTTP/native comparison matched status, both windows, and all 22 action rows exactly;
the complete local overlay left zero unknown fields at the response, window, and action levels.
Thus the APK proves the stable message types, first-party JSON supplies the newer field/value names,
and live evidence proves the separate orchestration gRPC alias. Both `grpc.reflection.v1` and
`grpc.reflection.v1alpha` returned `UNIMPLEMENTED`, and public GitHub code search plus exact-string
Web search found no leaked service descriptor.

Native `GetAccount` accepted empty request bytes, modeled locally by `google.protobuf.Empty`, and
returned an envelope with `Account` at field `1`. The exact remote request FQN is not recovered.
The response envelope decodes wire-compatibly with the existing local
`GetOrCreateAccountResponse` overlay without top-level unknown fields and exposes the same semantic
account fields as a live `GetOrCreateAccount` response, but its exact remote FQN has not been
recovered either. No Tailwind `GetAccount` binding occurs in the APK's AOT or reconstructed
protobuf/client trees, so the available Android static artifact cannot honestly name those two
remote types.

## Scrubbed decoded snapshots

The Standard/free profile had tier `1` and `computeMeteringEnabled=true`. After both bounded
generations settled, Web and Android decoded the same snapshot:

```json
{
  "status": 1,
  "windows": [
    {
      "kind": 1,
      "used_percent": 1.7261085,
      "remaining_percent": 98.2738915,
      "resets_at": "2026-09-05T07:37:21.548366Z"
    },
    {
      "kind": 2,
      "used_percent": 0.08219564285714286,
      "remaining_percent": 99.91780435714286,
      "resets_at": "2026-09-12T02:37:21.548437Z"
    }
  ],
  "selected_actions": [
    {"code": 3, "has_sufficient_quota": false},
    {"code": 9, "has_sufficient_quota": true, "cost_percent": 1.8666666666666667},
    {"code": 10, "has_sufficient_quota": true, "cost_percent": 2.3},
    {"code": 15, "has_sufficient_quota": true, "cost_percent": 15.7}
  ],
  "out_of_quota_actions": [3]
}
```

The reset timestamps differ because the server supplied independent timestamp values for the two
windows. They were not rounded or synthesized by the probe.

A later read-only recheck observed the Standard/free five-hour row at exhaustion: used `100` was
present and remaining `0` was elided. Web and Android matched. Every action became unavailable and
response `f3` simultaneously changed from `[3]` to `[1, 2, ..., 22]`. On unused Pro, every action
was available and `f3` was absent. The recovered declaration confirms that `f3` is repeated
`UserAction outOfQuotaActions`; it remains outside the public model because action rows already
carry the exact per-action quota-sufficiency signal.

The read-only Pro profile had tier `2`, `computeMeteringEnabled=true`, and unused sliding windows.
Web and Android action rows matched exactly; every advertised Pro cost was one quarter of the
corresponding Standard/free cost at this snapshot. No Pro generation was submitted, so Pro
reservation, settlement, and capacity were not observed.

## Bounded transition log

The Standard/free probe selected one explicit ready source in an owned notebook. It submitted one
Flashcards generation and one Quiz generation, waited for each, and deleted both artifacts.

| Event | Five-hour used | Weekly used |
| --- | ---: | ---: |
| initial | `0` | `0` |
| Flashcards submitted | `1.8666666666666667` | `0.08888888888888889` |
| Flashcards completed | `0.8685427666666666` | `0.041359179365079365` |
| Quiz submitted | `3.1685427666666666` | `0.1508829888888889` |
| Quiz completed | `1.7261085` | `0.08219564285714286` |
| artifacts deleted | unchanged | unchanged |

The first submission anchored both reset timestamps. The second did not move them. This proves the
two observed action families reserve their advertised percentage at submission and reconcile at
completion. It does not prove a general settlement formula or an indivisible provider credit.

`GetQuota` was read before and after these transitions. Its response did not change: most rows were
signed int64 max, while rows 4 and 5 were `75` and `100`. This is a separate 18-code feature
namespace, not the usage meter's action namespace.

## Evidence boundary for implementation

This document is durable research evidence, not a replay fixture. The admitted Android adapter
gives each recovered or wire-equivalent artifact a distinct governance owner:

- the exact `ListQuotaSummary` message subset enters the protobuf evidence ledger and generated
  exact-package sources; its live-field response overlay enters
  `grpc-runtime-parser-overrides.json` against the known response type;
- the orchestration `ListQuotaSummary` alias enters
  `grpc-service-signature-inferences.json`, not the APK-extracted gRPC signature inventory;
- `PremiumUserInfo.compute_metering_enabled = 7` enters the evidence ledger and admitted
  exact-package account overlay;
- the `GetAccount` empty-request and envelope-response codecs enter the schema-v2 path-only
  `grpc-service-signature-exceptions.json` entry, not the runtime-parser override registry.

Tests must preserve exact synthetic wire bytes for numeric, scalar-presence, empty-request, and
response-envelope behavior. Web integration coverage must use scrubbed VCR cassettes.

Authenticated parity probes are read-only and run in separate processes with an explicit profile.
They never select a fallback identity mid-operation and never generate artifacts in ordinary CI.
