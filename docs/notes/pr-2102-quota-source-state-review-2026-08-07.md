# PR #2102 review + source-add failure-mode research (2026-08-07)

**Subject:** PR [#2102](https://github.com/teng-lin/notebooklm-py/pull/2102) "fix(source): align
quota and source-state accounting" (closes #1962), head commit `5439e333`.

**Method:** Six-lens review (four native Claude specialists + agy/Antigravity + Codex, which failed
on a usage-limit error and produced nothing), followed by live empirical probing against the real
NotebookLM backend (two waves, ~24 distinct `source_add` failure modes) to verify claims that static
review alone couldn't settle. This document consolidates both.

---

## 1. Six-lens review synthesis

Lenses: `code-reviewer`, `silent-failure-hunter`, `pr-test-analyzer`, `type-design-analyzer` (native
Claude, parallel dispatch), `agy` (independent full pass), `codex` (no output — usage cap). Three
earlier bot rounds (chatgpt-codex-connector, claude[bot] via GitHub Action) had already landed fixes
for: ID-only-row state inference, `SourceCounts` public-module rebinding, and an empty-roster race in
`get_metadata()`. All three verified still-correct on this head.

### 🔴 Should fix before merge

1. **Quota enforcement silently goes fully inert on any settings-RPC failure.**
   `get_source_limit_or_none` (`_app/source_capacity.py:101-111`) catches bare `Exception` around
   `client.settings.get_account_limits()` and returns `None` on *any* failure. `require_available`
   (`source_capacity.py:65`) treats `limit is None` as "unlimited" — a no-op guard. `None` already
   means "account has no configured cap" elsewhere, so there's no way for a caller to distinguish
   "unlimited" from "we couldn't check." No doc mentions the collapsed sentinel.

2. **`_extract_notebook_source_counts` still fabricates confident zeros — the "no fabricated states"
   fix is incomplete.** `_types/notebooks.py:56-57,71-72`: when the sources block is absent/malformed,
   *or* present but every row is an id-less ghost, the function returns `SourceCounts()` (all zeros)
   instead of `None`. Serializes as a confident "0 active, 0 failed, 0 total" — contradicts the
   CHANGELOG's claim that compact rows "leave `source_counts` null rather than fabricating states."
   Only a genuinely empty `[]` should get real zeros.

Both verified directly against the code (not just reported by a lens).

### 🟡 Consensus findings (≥2 lenses independently)

3. **Quota preflight is bypassed at several call sites** (agy + pr-test-analyzer): MCP file uploads
   (check deferred until the actual byte POST), research bulk-import (zero capacity-check references
   anywhere in that path). Both are in the PR's own "Deferred polish findings," but nothing in-code
   marks them as known-accepted gaps (no comment/xfail/tracking issue).

4. **CLI is left out of the new capacity surface** (agy + type-design-analyzer + code-reviewer):
   `cli/services/source_listing.py:73` still calls bare `fetch_sources`; MCP/REST both moved to
   `fetch_sources_with_capacity`. `source list --json` never emits `source_counts`/`source_limit`/
   `remaining_capacity`, despite this repo having an explicit `test_cli_mcp_parity.py`.

### 🟢 Single-lens, worth a look

- Public SDK bypass (agy) — `client.sources.add_url/add_text/add_file/add_drive*` skip
  `ensure_source_capacity` entirely; only CLI/REST/MCP adapter paths got wired up.
- REST `add_file` spools up to 200MiB to disk before the capacity check runs (agy).
- Positional wire read hard-blocks legit adds with no override (code-reviewer) — `source_limit` comes
  from an undocumented positional descent (`_settings.py` `limits[2]` under path `(0,1)`).
- New RPC amplification on hot paths (code-reviewer) — every non-batch `source add` costs +2 POSTs
  (`sources.list` + `get_account_limits`, run concurrently via `asyncio.gather`); label-filtered
  `source_list` double-lists. See §3 below for the follow-up analysis.
- `quota_counted` is a permanent tautology of `active` (code-reviewer) — two fields that can never
  differ, documented as distinct in `docs/python-api.md`.
- Type invariants are convention-only (type-design-analyzer) — `SourceCounts`/`SourceCapacity` are
  frozen dataclasses with no `__post_init__`; nothing stops constructing an inconsistent instance.
- Unknown-status handling is inconsistent between `has_explicit_status` (→ `None`, "not explicit") and
  `SourceCounts.from_statuses` (documents degrading unknown statuses to `READY`) — same input shape,
  different outcome depending on which path parses it (agy).
- Test gaps: CLI's own tests never exercise the quota-rejection path; both Drive-add executors are
  untested for rejection (pr-test-analyzer).
- Minor (code-reviewer): truncated (not fully-empty) roster still overwrites `sources_count`;
  `SourceAddError.url` gets a notebook id instead of a URL (cosmetic); `asyncio.gather` without
  `return_exceptions` can orphan a settings POST; two independent `SourceCounts` serializers could
  drift; an autouse VCR fixture masks capacity regressions across the whole integration suite.

### ✅ Verified clean

Batch capacity-snapshot threading is race-free within a batch (dedup-by-id, no double-counting);
quota rejection correctly isolates per-item in a batch rather than aborting it (no wasted RPCs — a
pure local check); label-filtered listing computes capacity against the unfiltered roster correctly;
`_app`/MCP/REST layering stays clean; `mypy` clean; 441 touched tests pass.

---

## 2. Should we chase every finding, or even close the PR?

Two follow-up questions came up: (a) is the RPC-amplification finding worth fixing, and (b) given the
quota preflight is fundamentally racy across processes, should the PR be scrapped and rethought?

**(a) RPC amplification** — real, but a fast-follow not a blocker. *(Superseded: §4.7 found the limit
is already present in the `GET_NOTEBOOK` response `ensure_source_capacity` fetches, which removes the
extra POST outright rather than caching it. Read that first — the analysis below is retained for
context.)* `execute_source_add` calls
`ensure_source_capacity` unconditionally whenever no `capacity` snapshot is passed — i.e. every
non-batch add. No caching exists anywhere in `_settings.py`/`source_capacity.py`. Cheapest fix:
memoize `get_account_limits()` per client session (tier is effectively static) — cuts 2 extra RPCs to
1. The `sources.list()` half is not cheap to remove (the count must be fresh to be meaningful; no
lighter count-only RPC is known). A free bonus win in the same area: `source_listing.py:92-101`
double-lists (once for the label-filtered display, once inside `get_source_capacity` for the
unfiltered count) — should share one fetch.

**(b) The cross-process race** — real and structurally irreducible from the client side, but *not* a
reason to redesign. No CAS primitive exists in the captured RPC surface, and even a perfect same-host
lock (this codebase already has `filelock`-based cross-process coordination for auth/storage state)
couldn't help here, because the NotebookLM **web UI** is an independent writer to the same backend
resource that no client-side mechanism can ever reach. Any redesign implementing "prevent before
create" inherits the identical ceiling — the limitation is a property of the problem domain, not this
PR's implementation. This is honestly documented in `source_capacity.py`'s module docstring already.

**Verified against the original issue.** #1962's body (from a live Codex stress-test round, not
speculation) already establishes: *"Adding a source that fails... can still create a persistent
backend row discoverable via `source_list(status="error")`. It's not garbage-collected; the caller
must delete it,"* and its proposed fix direction — *"validate quota/preconditions before creating a
backend row"* — is exactly what this PR implements. Its proposed `source_counts` shape
(`active`/`ready`/`processing`/`failed`/`quota_counted`/`total_records`) is a near-verbatim match to
what `SourceCounts` ships. The `SourceAddError` message (`source_capacity.py:61-74`) verbatim
satisfies the issue's Part A acceptance criterion ("name the exact limit + current active count").
**Conclusion: don't close it.** Fix findings 1–2 above, ship the accounting layer (it's genuinely
complete and faithful to the bug report), treat findings 3–4 as tracked fast-follows.

Since prevention can never be airtight, accurate *after-the-fact* accounting is the real backstop —
which is what makes finding #2 (fabricated zeros) more load-bearing than its "IMPORTANT, not
CRITICAL" label suggests: it's the safety net for exactly the failure mode prevention can't close.

---

## 3. Live RPC probing — what the backend actually does on a failed add

Two probe waves against the real API (scratch notebooks, created and torn down each time), using a
patched response decoder (`notebooklm._client_seams._default_decode_response`) to dump raw wire
payloads. Scripts live in the session scratchpad, not committed.

### Wave 1 — single-cause probes (12 cases)

| Probe | RPC path | rpc_code | Client-visible result | Ghost row? | Final status |
|---|---|---|---|---|---|
| dead domain | `ADD_SOURCE` (`izAoDd`) | 9 | `SourceAddError` (sync) | yes | `ERROR` |
| 404 / 403 / 500 / empty-204 body | `ADD_SOURCE` | 9 (all four, identical) | `SourceAddError` (sync) | yes | `ERROR` |
| bad YouTube ID | `ADD_SOURCE` | 9 | `SourceAddError` (sync) | yes | `ERROR` |
| good URL (control) | `ADD_SOURCE` | — | success | — | `READY` |
| empty file (0B) / corrupt "PDF" / mismatched MIME | `ADD_SOURCE_FILE` (`o4cbdc`) | — | **success, no exception** | yes | `ERROR` (async, ~3s later) |
| unsupported `.exe` | `o4cbdc` register → upload POST | — | `ValidationError` (HTTP 400, **different exception class**) | yes | **`PREPARING` forever** |
| empty text | `ADD_SOURCE` | **3** (INVALID_ARGUMENT) | `SourceAddError` (sync) | **no** | — |

Findings:

- **`rpc_code` does not discriminate cause for URLs.** Dead domain, 404, 403, 500, and an empty body
  all return the identical `9`/"Failed precondition." No signal distinguishes "permanently dead" from
  "blocked/paywalled" from "transient 5xx."
- **File-content failures are a worse gap than URL failures.** `add_file` returns *successfully, with
  no exception*, for empty/corrupt/mismatched-MIME files — the register RPC always succeeds before
  content is inspected; validation happens async afterward. A caller without `wait=True` gets zero
  synchronous signal that the file actually failed.
- **Unsupported file types leave a permanent quota-consuming zombie.** The `.exe` case: registration
  succeeds, the upload body itself gets HTTP-400-rejected (`ValidationError`, not `SourceAddError`/
  `RPCError` — any future cleanup logic keyed only on `SourceAddError` would miss this), and the row
  is stuck in `PREPARING` — never resolves to `ERROR`. `SourceCounts.from_statuses` counts `PREPARING`
  toward `active`/`quota_counted`. This row silently and permanently eats a real quota slot and
  doesn't even show up under `source_list(status="error")`. Not covered by #2102 or #1962 — both are
  scoped around the `ERROR`-status ghost pattern specifically.
- **The one clean case (empty text) proves the backend *can* reject with zero side effects** when the
  failure is knowable without I/O (no fetch/parse attempt needed). It just doesn't do that for
  anything requiring a fetch or content-parse — those need a row to exist first as an attachment point
  for the eventual result.
- **Ghost-row creation is immediate, not a delayed/probabilistic artifact.** Verified present on the
  first `source_list` check and stable through 15s of polling — confirming this is deterministic
  backend behavior, not a rare timing quirk.

### Wave 2 — structural probes (batch, duplicates, Drive, schemes, timeout, content-type, concurrency)

> **Correction (2026-08-13):** The batch-contamination conclusion below did not
> survive a controlled retest. The supposed good control URL also failed when
> added alone. Retesting with independently verified controls showed that
> `ADD_SOURCE` is per-item, admits good entries regardless of bad-neighbor
> position, and silently omits failed rows unless every entry fails. The quota
> boundary likewise admitted exactly the first entries that fit. See #2110 and
> #2115; the historical observation is retained below only as audit context.

- **Batching contaminates good sources with bad ones.** Sent a single raw `ADD_SOURCE` RPC with two
  entries (one good URL, one 404) by bypassing the client's one-source-per-call policy (the RPC method
  is documented as "batch-capable" in `rpc/types.py`, this client just doesn't use that). The whole
  RPC failed, and **both** sources — including the one that should have succeeded standalone — landed
  as `ERROR`. This is the most consequential wave-2 finding: it retroactively validates, with real
  evidence, why this codebase's batch adapters (`mcp/tools/sources.py::_add_url_batch`,
  `server/routes/sources.py::add_batch`) loop one RPC per source instead of using the wire protocol's
  real batch capability — a true batch call fails atomically at the row level and punishes unrelated
  good sources caught in the same call. **Worth a code comment on `RPCMethod.ADD_SOURCE` or in
  `source_capacity.py`'s docstring so a future "let's batch for efficiency" change doesn't reintroduce
  this.**
- **Idempotent retry self-healed under a genuine transport timeout.** A slow-URL probe (artificial
  10s server delay) tripped an actual 30s transport timeout → `RPCTimeoutError` → the existing
  probe-before-retry path in `idempotent_create` → the retry succeeded cleanly (`PREPARING`, no
  duplicate, no exception surfaced). Real-world validation of that machinery, not just unit tests.
- **URL-add supports PDFs, not raw images.** A direct PDF URL succeeded and was correctly imported
  (`READY`, title auto-derived). A direct PNG URL got the same generic code-9 rejection as a dead
  link. A previously-undocumented content-type boundary specific to the URL-add path (distinct from
  file upload) — worth a line in `docs/python-api.md` or `docs/troubleshooting.md`.
- **No client-side URL scheme validation.** `javascript:`, `ftp://`, `file:///etc/passwd`, and a
  `data:` URL all passed through untouched to the RPC (which correctly rejected all four with code 9).
  No local dereferencing happens — notebooklm-py never opens `file://` locally, so no SSRF/LFI risk on
  this client's own host — but each one wastes a round trip and creates a nonsense-titled ghost row.
  A cheap `urlparse(url).scheme in ("http", "https")` pre-check would skip both costs.
- **No dedup on repeated failures.** The same bad URL added twice created two independent ghost rows.
  Unlike the successful-add idempotency path (probes before retrying), a failing add has no equivalent
  safety net — repeated retries of a still-bad URL pile up ghosts without bound.
- **Drive-source errors don't propagate `rpc_code` consistently.** The invalid-Drive-file-id probe hit
  the same code-9 at the RPC layer (visible in the raw capture), but `SourceAddError.cause.rpc_code`
  came back `None` — the Drive add path's exception handling doesn't carry the code through `.cause`
  the way URL/text paths do. Relevant if `rpc_code` passthrough (see §5) is ever built.
- **Burst of 5 concurrent bad adds** all got the same code 9 — no distinct rate-limit signal (code
  8/RESOURCE_EXHAUSTED) appeared at this scale. Inconclusive; deliberately didn't push higher to avoid
  stress-testing the live service.

---

## 4. Android binary cross-check — what the first-party client knows

Third investigation wave: rather than probe the backend, ask Google's own mobile client what the
backend's contract *is*. Sources: the recovered mobile schema (`docs/mobile/schema.proto`, 282
messages / 767 fields, produced by the blutter port documented in `docs/mobile/endpoints.md`) and
string/symbol mining of `libNotebookLM_prod_android_library_flutter_artifacts.so` from
`notebooklm.apk/split_config.arm64_v8a.apk` (app v1.46.7). Dart AOT symbol names carry a per-library
id suffix (`@1877180633`), so grouping on it reconstructs each file's method surface without a full
decompile.

### 4.1 The ghost row is the documented protocol, not a leak

Mobile has an RPC the web client doesn't: **`AddTentativeSources`**, and the schema makes the two-phase
binding explicit:

```protobuf
message AddTentativeSourcesRequest {
  repeated TentativeSourceMetadata tentativeSourcesMetadata = 1;  // just { string name }
  string projectId = 2; ...
}
message AddTentativeSourcesResponse { repeated Source tentativeSources = 1; }

message UserContent {                    // the AddSources (phase 2) payload
  ...
  SourceId tentativeSourceId = 9;        // ← rebinds phase 2 to the phase-1 row
}
```

The row is created **before any content is inspected**, and `tentativeSourceId` re-attaches the real
content afterward. This confirms wave-1's inference (§3, "those need a row to exist first as an
attachment point") as the backend's actual design. Our web file path is the same shape:
`_source/upload.py:400-432` — `_register_file_source_for_upload` → `start_resumable_upload` → body
stream → `wait_until_registered`.

### 4.2 `PREPARING` is really `TENTATIVE` — this rewrites the wave-1 `.exe` finding

The binary's `SourceStatus` enum has six values; `rpc/types.py:437-440` models four. **Numbering read
directly out of the decompiled object pool** (`out/full/pp.txt` — each `ProtobufEnum` instance carries
its literal value at `off_8`), so this is measured, not inferred:

| # | Backend name | `rpc/types.py` |
|---|---|---|
| 0 | `SOURCE_STATUS_UNSPECIFIED` | — |
| 1 | `SOURCE_STATUS_PENDING` | `PROCESSING` |
| 2 | `SOURCE_STATUS_COMPLETE` | `READY` |
| 3 | `SOURCE_STATUS_ERROR` | `ERROR` |
| 4 | `SOURCE_STATUS_PENDING_DELETION` | **unmapped → `"unknown"`** |
| 5 | `SOURCE_STATUS_TENTATIVE` | `PREPARING` |

Independently corroborated positionally: `rpc/types.py` documents status at `source[3][1]`, and the
wire proto is `Source.settings` (tag 4 → index 3) → `SourceSettings.status` (tag 2 → index 1). Exact
match — the web JSON array and the mobile protobuf are the same message, so mobile field tags are a
reliable decoder for web positional indices generally.

**Mobile collapses these six wire states into four UI states.** A second, client-side `SourceStatus`
Dart enum exists (`unknown=0, loading=1, complete=2, failed=3`), and the mapping function
`_extension#23.toSourceStatus` (`persistence_proto_util.dart`, `0x10b8b58`) disassembles to:

| Wire | UI |
|---|---|
| `PENDING` (1), `PENDING_DELETION` (4), `TENTATIVE` (5) | `loading` |
| `COMPLETE` (2) | `complete` |
| `ERROR` (3) | `failed` |
| `UNSPECIFIED` (0), anything else | `unknown` |

So the first-party client deliberately shows a stuck `TENTATIVE` row as an indefinite spinner. It has
no notion that the row is dead — which is exactly the `.exe` symptom, and confirms there is no
server-side signal we're failing to read.

Consequences:

- **The `.exe` zombie is not "stuck in a processing stage" — it is a row still in phase 1 that was
  never committed.** Registration created the tentative row, the upload body was rejected at HTTP 400,
  and the phase-2 commit never ran. Strictly sharper than §3 could state, and it predicts the row is
  permanently inert (nothing exists to advance it), which matches the observed behaviour. It also means
  `PREPARING` is *structurally* ambiguous in our model: it conflates "upload in flight, will resolve"
  with "half-finished transaction, will never resolve" — the two cannot be told apart by status alone.
- **Status `4`/`PENDING_DELETION` is a real state we don't model**, and `source_status_to_str`
  (`rpc/types.py:465`) degrades it to `"unknown"`. A source observed mid-deletion falls through every
  status branch. Separate from #2102's scope; worth its own small issue.

### 4.3 There is no per-source failure reason anywhere in the schema

Across all 282 recovered messages, `Source`, `SourceMetadata`, and `SourceSettings` carry **no error,
reason, or failure-detail field**; `AddSourcesResponse` returns bare `Source` objects. This settles
§3's `rpc_code` finding from the opposite direction: it isn't that the code is a coarse bucket, it's
that the backend has **no per-source failure channel to expose**.

The mobile UI's entire add-failure vocabulary corroborates it:

| l10n key | Cause |
|---|---|
| `ProjectL10n\|maxSourcesReached{SnackbarMessage,DasherUserMessage,NonDasherPlusMessage,NonDasherNonPlus{Owner,Writer}Message}` | quota (4 tier/role variants) |
| `ProjectL10n\|get#fileTooLargeSnackbarMessage` | file size |
| `ContentPickerL10n\|get#invalidYoutubeUrlSnackbarMessage` | **client-side** pre-check |
| `ContentPickerL10n\|get#unsupportedDriveFileTypeMessage` | **client-side** pre-check |
| `ProjectL10n\|get#couldNotAddSourceSnackbarMessage` | generic catch-all |

There is no "couldn't fetch that URL" string. Google's own first-party client cannot distinguish a
paywall from a dead domain either — this is a hard ceiling on any `rpc_code` passthrough design, not a
gap in this client. Note also that two of the five are *client-side* validations, which supports the
cheap-pre-check idea in §5.

### 4.4 Quota is signalled by a short response, not by an error

A log string recovered verbatim from the binary:

> `Server returned fewer tentative IDs than requested. This will happen when user reach the source upload limits. In this case, intentionally to skip id updates for sources.`

`AddTentativeSources` **silently returns fewer rows than requested** when the cap is hit — no error
code, no `RESOURCE_EXHAUSTED`. That is very likely why §3's 5-concurrent-bad-add burst never surfaced
code 8: that isn't the channel quota uses.

Additionally, the limit is **discoverable rather than empirical**: `Project.projectTierLimits` is a
`TierLimits` message returned on *every* `GetProject`. Exact tags, from the decompiled `BuilderInfo`:

```protobuf
message TierLimits {          // google.internal.labs.tailwind.orchestration.v1 (wire)
  int32 maxProjects          = 2;
  int32 maxSourcesPerProject = 3;
  int32 maxWordsPerSource    = 4;
}
```

There is no tag 1 — the builder starts at 2, so no field is missing. (The app's *local persistence*
copy of `TierLimits` uses identical tags but renames tag 2 to `maxProjectsOfProjectOwner`.) Note
`maxWordsPerSource` is a limit we don't model or surface anywhere.

We don't read this at all today — `_settings.py` instead derives `source_limit` from a separate
undocumented positional descent (`limits[2]` under path `(0,1)`), flagged as brittle in §1 — **which
§4.7 confirms is this very `TierLimits` message**, reached from a different RPC.

> **Parser bug found along the way.** `docs/mobile/schema.proto` renders all three of these as
> `int32 fieldType = N`. That's an artifact of `scripts/parse_pbschema.py`: the Dart `BuilderInfo`
> const list contains a literal placeholder string `"fieldType"`, and the parser picks that up instead
> of the adjacent real name. Any `fieldType` in the committed schema is a **parse failure, not a real
> field name** — the true names are recoverable from the `asm/` output as shown above. Bounded:
> **11 occurrences out of 767 fields** (~1.4%), so the rest of the schema is unaffected.

### 4.5 Mobile doesn't *delete* ghost rows either — it reconciles around them

The add-source helper library (`@1877180633`) is **eight symbols total**: `_addDriveSourcesToProject`,
`_addFileSourcesToProject`, `_addTextSourcesToProject`, `_addPlayBooksSourcesToProject`,
`_handleAddSourceError`, `_quotaExceededSnackbarContent`, `_logger`, `init:_logger`. The entire error
path is log + snackbar. The only `DeleteSources` caller in the whole binary is `_handleDeleteSource`
(`@1880355218`) — the user tapping delete.

Instead, mobile leans on `_reconcileOptimisticSources` + `_startPeriodicRefresh`/`_stopPeriodicRefresh`
(notebook-detail view model, `@1880355218`): tentative rows are treated as optimistic UI — the app even
synthesizes client-side placeholder titles (`UserContent|get#{pdfFile,imageFile,driveFile,audioFile,
youtubeVideo}OptimisticSourceName`) — and reconciled against polled server truth. **Google's answer to
the ghost-row problem is display reconciliation, not deletion.** The debris stays. This doesn't
invalidate the §5 `cleanup_on_failure` idea, but it does confirm no server-side GC exists to wait for,
and that opt-in (not default) is the right posture. §4.6 pins down the exact mechanism — the cleanup
that does happen is purely local.

### 4.6 What the failure path actually does (disassembled)

The first pass could only list symbol names. With the analysis build, the two relevant functions
decompile to concrete call sequences.

**`_handleAddSourceError`** (`project_list_util.dart:631`) — in order:

1. `SafeLogger::atWarning` — log only.
2. `TailwindService::getProject` — **re-fetch the project from the server** (with a
   `RequestExecutionMode`), i.e. go get ground truth rather than trust local state.
3. `ProjectBuilder::sourceItems` → `ListBuilder::removeWhere` (+ `ListBase::contains`) — **drop the
   optimistic rows from the local model.**
4. `SnackbarDisplayController::showInfoSnackbar` — note **info**, not an error toast.

This meaningfully refines §4.5. Mobile *does* clean up after a failed add — but **only the client-side
copy**. There is no `DeleteSources` call anywhere on this path; the backend row is left exactly where
it is. So the mobile behaviour is "re-sync from server, discard my optimistic guesses, tell the user
mildly" — the ghost row survives, and the user is never told a row leaked. The whole failure path is
also *unconditional*: it does not branch on error type at all, which is consistent with §4.3 (there's
no per-source reason to branch on).

**`_registerTentativeSources`** (`project_component.dart:7406`) — calls
`SourceService::addTentativeSources`, then on the short-response quota case (§4.4) does
`SafeLogger::atWarning` + `showInfoSnackbar` + `Store::upsertProject` / `_onProjectUpdated`. No retry,
no error raised, no distinction from a normal partial result — it simply persists whatever came back.

**Quota copy confirms role/tier-aware remediation.** The four `maxSourcesReached*` variants resolve to
distinct user guidance, which is a genuine product decision worth mirroring if we ever improve
`SourceAddError`'s message:

| Audience | Copy |
|---|---|
| Enterprise/Dasher | `…source limit.` (no remediation offered) |
| Writer, not owner | `…source limit. Contact the owner of the notebook to upgrade.` |
| Owner, upgradeable | `…source limit. For more, <upgrade-link>upgrade</upgrade-link>.` |

Separately, a distinct `You've reached the usage limit. Please try again later.` string exists — a
rate-limit message, not a source-count one. Relevant to §6's open question about whether a true
RESOURCE_EXHAUSTED path exists: the app clearly has copy for one, even though the 5-add burst in §3
never triggered it.

### 4.7 Confirmed on the web wire: `GET_NOTEBOOK` carries the same `TierLimits`

Checked against the committed VCR cassettes (`tests/cassettes/*.yaml`, 35 contain `rLM1Ne`), decoding
the batchexecute envelope directly. **Answer: yes.**

`GET_NOTEBOOK` (`rLM1Ne` → `GetProject`) returns the tier block at **top-level index 10 = proto tag 11**
— exactly where mobile's `Project.projectTierLimits = 11` says it should be:

```
[10] (tag 11) → [6, 500, 300, 500000, 2]
[9]  (tag 10) → [true, true, true]        # PremiumFeatureInfo's 3 bools, also as predicted
```

Decoded with the mobile field names (§4.4), against a Pro account:

| idx | tag | mobile name | value |
|---|---|---|---|
| 0 | 1 | *(not in mobile schema)* | `6` |
| 1 | 2 | `maxProjects` | `500` |
| 2 | 3 | `maxSourcesPerProject` | `300` |
| 3 | 4 | `maxWordsPerSource` | `500000` |
| 4 | 5 | *(not in mobile schema)* | `2` — matches `AccountLimits.tier` 2 = Pro |

These are exactly Google's published Pro limits (500 notebooks / 300 sources / 500k words), so the
mapping is externally corroborated, not just internally consistent.

**It is the same block `_settings.py` already parses.** `GET_USER_SETTINGS` (`ZwVcOc`) at path `(0,1)`
returns `[6, 500, 300, 500000]` — identical values, and `_NOTEBOOK_LIMIT_INDEX=1` /
`_SOURCE_LIMIT_INDEX=2` / `_TIER_INDEX=4` line up with `maxProjects` / `maxSourcesPerProject` /
tier. So the "undocumented positional descent" flagged in §1 isn't a mystery shape — **it is
`TierLimits`, and mobile just gave us its field names.** That alone de-risks the §1 finding without
changing any code: the indices can be documented and named instead of left as magic numbers.

**Two bugs this surfaces:**

1. `_settings.py:17` comments index 3 as `max_characters_per_source`. Mobile names it
   **`maxWordsPerSource`**, and Google documents 500,000 **words** per source. The comment is wrong;
   the field is words, not characters.
2. ~~In both captured `GET_USER_SETTINGS` responses the limits list has only 4 elements, so `tier`
   comes back `None`.~~ **REFUTED by live probing (§4.8).** Live `get_account_limits()` returns a
   **5-element** list with `tier` populated (`raw_limits=(1, 100, 50, 500000, 1)`, `tier=1`). The
   4-element responses are an artifact of those specific older cassettes, not current backend
   behaviour, and `_TIER_INDEX` is not dead code. Only the `maxWordsPerSource` mislabel (item 1) is a
   real bug.

**Caveat before acting on this.** Presence is not guaranteed — but it is far more reliable than a
first, buggy pass suggested. Across **all 42** captured `GET_NOTEBOOK` frames, **41 carry the block**.
The sole absence is `notebooks_create.yaml`, a fetch immediately after creating a notebook, which
returned `null` at **both** index 9 and index 10. Request params are byte-identical across cassettes
(only the notebook id differs), so this is server-side lazy population on a fresh notebook, not a
request-shape artifact. A switch to reading limits from `GET_NOTEBOOK` still needs a **fallback to
`get_account_limits()` when index 10 is null** — but that fallback fires on ~2% of calls, not ~17%.

The block also appears in **two shapes**: `[6, 500, 300, 500000]` (24 frames) and
`[6, 500, 300, 500000, 2]` (17 frames). So the trailing `tier` element is genuinely **optional on the
same account**, and `_settings.py`'s `len(limits) > _TIER_INDEX` guard is correct and load-bearing —
not dead code.

> **Numbers corrected 2026-08-07.** An earlier pass reported "5 of 6 frames" here. That was wrong: the
> cassette frame-splitter treated batchexecute's **byte** length prefix as a character count, so any
> response containing non-ASCII was silently dropped. `rLM1Ne` actually has **42** frames, not 6, and
> `hizoJc` decoded as "empty" when it was not. Tooling fixed; every count in §4.7 is from the full set.
> Flagged by the source-domain audit — a good catch against my own tooling.

**Impact on §1 and §2a.** With that fallback, the §2a RPC-amplification finding largely dissolves:
`ensure_source_capacity` already calls `sources.list()`, which *is* `GET_NOTEBOOK` (`listing.py:46`),
so the limit arrives in a response we are already paying for — the extra `get_account_limits()` POST
becomes a rare fallback rather than a per-add cost. That is a better fix than the memoization proposed
in §2a, and it composes with it (memoize the fallback too). This is now the recommended direction for
both findings.

### 4.8 Live cross-transport check: web and mobile gRPC agree exactly

Ran the same account against **both** transports live (not cassettes), using a mobile gRPC harness
built for this (see §4.10):

| | tag 1 | `maxProjects` | `maxSourcesPerProject` | `maxWordsPerSource` | tier |
|---|---|---|---|---|---|
| Web `get_account_limits()` — live | 1 | 100 | 50 | 500000 | 1 |
| Mobile gRPC `GetOrCreateAccount` `[1.2]` — live | 1 | 100 | 50 | 500000 | 1 |

**Field-for-field identical.** This is the strongest available confirmation that the block
`_settings.py` parses positionally IS the `TierLimits` message, and that our index constants are
correct — the same values arrive tag-addressed over gRPC, where there is no positional ambiguity to
get wrong.

Three consequences:

1. **`tier` is populated and `_TIER_INDEX` works.** Live returns 5 elements with `tier=1`. The
   4-element cassette responses were stale samples, not current behaviour — correcting §4.7 item 2.
2. **The Pro figures `[6, 500, 300, 500000, 2]` in the cassettes come from a different, Pro-tier
   account**, not from a transport difference. Both live profiles here are free tier
   (100 notebooks / 50 sources / tier 1), matching Google's published free limits, exactly as the Pro
   cassette values match the published Pro limits. Two tiers, two transports, all four consistent.
3. **`maxWordsPerSource = 500000` is tier-independent** — identical on free and Pro. Reinforces that
   it is a per-source content limit, and that `_settings.py:17` calling it "characters" is wrong
   (§4.7 item 1).

### 4.9 Confidence

Everything in §4 is now **measured**, not inferred. The two open items from the first pass (enum
numbering, `TierLimits` tags) were closed by rebuilding blutter *with* code analysis and reading the
object pool and `BuilderInfo` disassembly directly, and the failure path in §4.6 is read off real
disassembly rather than guessed from symbol names.

The rebuild is preserved at **`~/src/blutter-nblm/`** with a `README.md` recording the full build
identity and a from-scratch reproduction recipe. Verified build inputs:

| | |
|---|---|
| App | NotebookLM `1.46.7.940945420`, `.so` sha256 `082d75e3…b8e6f81f` |
| Dart SDK | `3.13.0-256.0.dev` @ `bf06ec43d9f241a04072fc97ed3f2501ce935d7d` |
| Snapshot hash | `80d3c83b83e625573b88d3775debfe7d` |
| blutter base | `528acbe83ba35a3a53fb97b231cb5f968c7068d1` + `docs/mobile/blutter-dart3.13.patch` |

Two corrections to the prior reversing notes: the patch **applies cleanly to current blutter HEAD**
(no rebase needed), and the build is **~3 minutes, not multi-hour** — the Dart SDK sparse clone is only
71 MB. `docs/mobile/endpoints.md` and the `mobile-binary-reversing` memory should be corrected.
The one analysis error emitted (`handleArgumentsDescriptorTypeArguments`, `insn.IsBranch()`) is
non-fatal and did not affect any output used here.

Nothing here contradicts §3's batch-contamination finding — the mobile schema is batch-shaped
throughout (`repeated UserContent`, `repeated TentativeSourceMetadata`), so atomic per-batch failure
remains the backend's real semantics and the one-RPC-per-source architecture note stands.

### 4.10 Live mobile-gRPC probing — how, and one very costly gotcha

The mobile gRPC API can be called directly from Python, which makes the mobile surface testable
rather than merely readable. This closes the loop on §4: claims can now be confirmed against the live
backend on **both** transports.

**Auth** — exactly as `docs/mobile/auth-research.md` documents, and it is still
accurate. `gpsoauth.perform_oauth()` with the NotebookLM Android identity
(`app=com.google.android.apps.labs.language.tailwind`,
`client_sig=a3382adf91991e6ef1e7e7de309c1febfedf3283`) and the **full 10-scope Android bundle** yields
a 432-char `ya29` bearer. The `labs-tailwind` scope alone mints fine but then fails the actual call
with `grpc-status 7 PERMISSION_DENIED` — a failure that only shows up at request time, not at mint
time.

**The gotcha, which cost an hour:** the request `content-type` must be plain **`application/grpc`**.
Sending `application/grpc+proto` makes Google's ESF frontend return an **HTML 404** — which reads
exactly like a wrong host or an unregistered method path, and sends you hunting through hostnames and
API keys. Ruled out along the way (all irrelevant): 4 candidate hosts, and all 4 `AIza…` keys embedded
in `base.apk` as `x-goog-api-key`. Diagnostic tell: a *correct* request to a *wrong* service returns
`HTTP 200` with `grpc-status: 12` (UNIMPLEMENTED) — as `notebook-pa.googleapis.com` does — whereas a
content-type mismatch returns an HTML 404. **A 404 here means content-type, not routing.**

```
POST https://notebooklm-pa.googleapis.com/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/<Method>
authorization: Bearer <ya29…>
content-type: application/grpc          # NOT application/grpc+proto
te: trailers
body: b"\x00" + uint32be(len(msg)) + msg
```

No `x-goog-api-key` is required. The host `notebooklm-pa.googleapis.com` is confirmed correct by real
intercepted app traffic (`scripts/capture_mobile_grpc.js:15`), so the earlier suspicion of endpoint
drift was unfounded — the documented endpoint works unchanged.

Harness used for §4.8 lives in the session scratchpad (`mgrpc.py`: protobuf encode/decode, gRPC
framing, bearer minting, `NB_PROFILE` selection). Not committed — it handles credentials, and it
should stay out of the repo unless it is reworked to reuse `_auth/` rather than read
`master_token.json` directly.

---

## 5. Design implications for follow-up work

None of this has been implemented yet — captured here as scoped ideas for later, informed by what the
probing actually showed (several of these are weaker than they looked before probing; noted below).

- **`cleanup_on_failure` option for `source_add`.** Buildable: `ensure_source_capacity` already
  lists sources before a create attempt (for the quota check), so a "before" snapshot is often already
  in scope for free. On failure, diff against a fresh `list(status=ERROR)` and delete only what's new
  (never blind-delete-all-ERROR — that would nuke a stub the user left there on purpose to inspect).
  Needs to be **three different mechanisms**, not one, per the probing: (1) hook off `SourceAddError`
  for the URL path (synchronous, works as designed), (2) needs `wait`-style polling for the
  file-content-failure path (no synchronous exception exists to hook there), (3) needs to also catch
  `ValidationError` (not just `SourceAddError`) for the unsupported-file-type path — the one that
  actually matters most, since it's the only ghost shape that silently inflates quota. Recommend
  opt-in (not default), to preserve today's debuggability.
- **`rpc_code` passthrough on `SourceAddError`.** Cheap (data already flows onto `.cause`, mostly
  needs a forwarding property + message line) but weaker than first proposed: probing showed code 9 is
  a single bucket for *all* URL-fetch-failure causes (dead domain, 404, 403, 500, empty body) — it
  won't power a smart "worth retrying as text" decision for URLs. Still useful as a coarse
  "input rejected outright (3) vs fetch/parse attempted and failed (9)" signal, and for programmatic
  handling instead of string-matching the generic message. Needs fixing in the Drive path too (see
  above) to be consistent across source types.
- **`text_fallback` param for `add_url`** (pairs with the cleanup option): try the URL, and on
  `SourceAddError`, clean up the ghost stub and add caller-supplied pre-fetched text as the
  replacement in one call, instead of two manual steps. Deliberately *not* proposing the library
  auto-fetch content itself as a fallback — if a CDN/anti-bot layer blocked Google's own fetcher,
  there's no strong reason to expect this client fetching from a different IP/UA would fare better,
  and it adds a new failure/security surface for uncertain benefit.
- **Doc note on the URL/PDF content-type boundary** (PDF via URL: yes; image via URL: no).
- **Superseded: doc/comment note on batch-RPC contamination.** The corrected
  controlled retest described above established per-item admission rather than
  atomic contamination. #2115 therefore uses the true batch RPC for the already
  batch-shaped MCP/REST endpoints while retaining single-item add separately.
- **Optional: cheap scheme pre-validation** on `add_url` to skip obviously-non-http(s) input before
  spending an RPC round trip and creating a ghost row.

## 6. Open / unresolved

- Whether an actual quota-boundary rejection (the 51st source against a real limit) produces the same
  `rpc_code=9` as the generic fetch-failure cases, or something distinguishable — not tested (would
  require filling a real notebook to its limit; judged not worth the cost/waste for this pass).
  §4.4 makes a concrete prediction to test against: on mobile the cap manifests as a *short response*
  (fewer rows than requested) rather than any error code, so the web path may likewise return success
  with nothing created rather than an `rpc_code` at all.
- Whether higher-volume concurrent bursts would surface a distinct RESOURCE_EXHAUSTED (8) code — burst
  of 5 didn't trigger it; not pushed further to avoid stress-testing the live service. §4.4 suggests
  this may be a dead end: quota doesn't appear to use an error code on this surface.
- ~~Whether the web `GET_NOTEBOOK` response carries the `TierLimits` block~~ — **resolved: it does**,
  at index 10 (§4.7). Remaining sub-question: how broadly the null-on-freshly-created case applies, and
  whether `GET_USER_SETTINGS` ever returns the 5th (tier) element in real traffic — both captures had
  only 4, the unit tests assume 5.
- ~~Exact `SourceStatus` numbering and `TierLimits` field↔tag pairing~~ — **resolved** by the blutter
  rebuild (§4.2, §4.4, §4.8).
- How many other `fieldType` entries in `docs/mobile/schema.proto` are parser failures rather than
  real names (§4.4) — `scripts/parse_pbschema.py` needs a fix, and the committed schema a re-run.
