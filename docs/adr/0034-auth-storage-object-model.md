# ADR-0034: Auth storage object model and incremental extraction

## Status

Accepted. The Phase 12C owner extraction is complete. This ADR amends [ADR-0033](0033-auth-consolidation-policy.md), whose consolidation removed cap-induced seams but left independently owned state, lifetimes, and reasons to change in `storage.py`.

## Context

At `87227de1` on 2026-08-08, `_auth/storage.py` is exactly 3,102 lines and owns lock registries, atomic credential I/O, cookie CAS, raw document policy, account migration, promotion workers, full replacements, master-token persistence, and compatibility templates.
Its exact ceiling and slack lock mean additions red CI. Seven raw writers exist (six profile intents plus arbitrary-path `write_master_token`), transaction policies are 3 raise / 1 skip / 2 report,
six physical shims remain, and the corrected patch ledger records 280 sites (171 public, 109 private; storage 27/26).

The static graph has 26 direct modules / 13,745 lines and 68 scoped edges (54 module, 14 function-local). There is no module-only SCC; all scopes produce `cookies, keepalive, master_token, psidts_recovery, storage`.
A safe split must preserve opaque JSON, per-intent corruption and lock behavior, monkeypatch timing, and v0.x identity while shrinking the facade in every production stage.

## Decision

Extract by owned state/invariant, not headings. `A -> B` below means A may depend on B.

| Component | Owner / lifetime and state | Invariant | Dependencies |
|---|---|---|---|
| `ProfileStore` | Per raw caller path; separate canonical ordering key; no cached document | One aggregate commit boundary, per-intent locks, lossless round-trip; raw path controls I/O and locks | Down to document, typed I/O, locks, merge, derived token file; never migrator, scheduler, network, facade |
| `ProfileDocument` | Immutable decoded value | Preserves unknown root/namespace keys, origins, raw rows; decode chooses no corruption policy | Values only; no I/O, locks, lifecycle, orchestration |
| `cookie_filter.py` | Stateless pure filter and value-free diagnostic boundary | Filters raw capture rows without retaining values or mutating input; owns no path, lock, document, commit, or lifecycle state | Cookie policy/semantics and logging only |
| `credential_io.py` | Stateless leaf; two typed wrappers over one unchecked bypass | Profile and arbitrary token writes cannot be confused; no other bypass importer | Atomic I/O and values only |
| `StorageLockManager` | Shared process default or injected isolate; owns raw-path locks, registry, OS gateway, warning-once | Same raw lock path serializes stores; only cookie CAS blocks; secure-parent prep stays operation-specific | Lock primitives/values only |
| `CookiePersistence` | Per client; typed baselines by canonical path plus a legacy projection adapter | Baselines never cross profiles; outcome table alone advances order; closes with client | Store, snapshots, thread-dispatch seam |
| `LegacyAccountMigrator` | Stateless service over store and legacy-context collaborator | Owns two-file resolve/promote/scrub and two-read anti-race sequence | Store/account values and dedicated context I/O leaf |
| `LegacyPromotionScheduler` | Process-default canonical active-path registry, daemon workers, injected isolates | No 90-second write deadline in per-RPC reads; concurrent reads share one worker; settled failures are retryable; bounded exit drain is reported when incomplete | Store, migrator, thread/exit primitives |
| `LoadedAuth` | Closed `InlineLoadedAuth | FileLoadedAuth` value | File result always carries exact auth/store/baseline; inline carries neither store nor baseline | Loader outputs and immutable values |
| `SessionSeedLoader` | Concrete per-attempt service | Initial `HEAL_THEN_NAME_ONLY`, recovery CAS, reread, post-heal baseline precede acquisition | Source, store, browser/recovery leaf, values |
| `StoredAuthLoader` | Concrete per-load application service | Source/store/baseline remain paired through seed, acquisition, merge, result | Seed loader, sole `TokenAcquirer` protocol, migrator, scheduler, source/store/values |
| `AccountRouteResolver` | Concrete source-aware resolver per attempt | File account re-resolves after replacements; inline parses once; acquisition records final route | Source, migrator, scheduler, store/account values; acquirer depends downward |
| `LoginProfileWriter` | One command operation | Failed login write does no legacy reconciliation; success reconciles outside storage lock | Store, migrator, request/result/account values |
| `AccountMetadataWriter` | One account operation | Failed write leaves sibling; success scrubs. Clear attempts in-band first, then scrubs the sibling after returned/no-op failures; naturally propagated exceptions abort before scrub | Store, migrator, account values |
| `MasterTokenFile` | Per explicit token path, even one named `storage_state.json`; direct construction is legacy-adapter-only | Models an arbitrary legacy path without a fake profile; its replace is deliberately unchecked and performs no read-before-replace. Arbitrary production callers must derive it from `ProfileStore` and cannot independently pair token/profile paths | Typed token I/O, locks, codec, token value |
| `MintService` | Per network attempt | Owns the sole OAuth exchange plus web MergeSession/RotateCookies; never profile I/O | Network gateways, immutable `OAuthClientSpec`, secret-safe `MintedOAuthToken`, and `MasterToken` |
| `MasterTokenBootstrapper` | Per bootstrap; mint service, one store, bootstrap lock, verifier | Recheck after acquisition; session commits before token; paths cannot be independently paired | Token persistence only through its store; never receives a token file |
| `SingleFlight` | Process default or injected isolate | Atomic stale-epoch/flight claim; prompt-pop; cancellation settles without cancelling siblings; quiescent reset rejects live work | Thread/future/asyncio primitives only |
| `ColdRecoveryState` | Process default or injected isolate | Synchronizes weak-loop path locks and success generations; reset rejects locked paths | Async/thread/weakref primitives only |
| `ColdRecoveryCoordinator` | One cold token-fallback operation | Claims once, spells L2.5/L3/L4, preserves exact paired baselines/tracebacks, scrubs eleven callbacks on every exit | Injected refresh closures, state, single-flight |
| `RotationState` | Process default or injected isolate | Per-loop/path locks and atomic stamp-before-POST throttle; raw globals are identity views only | Async/thread/weakref primitives only |
| `AccountRepairService` | One Playwright repair operation | Claims before await, offloads only load, synchronously writes/clears, scrubs six callbacks on every exit | Account values, store/migrator/writer factory; call-time cookie/keepalive composition |
| `storage.py` facade | v0.x compatibility module | Old signatures, identities, defaults, results, odd policies, and patch seams remain observable | Delegates downward only; no new state or algorithms |

Cross-cutting rules are normative:

- `ProfileDocument` is lossless; each operation owns its distinct corruption policy outside decode.
- Account directives are closed `KeepAccount | ClearAccount | SetAccount`; adapters retain existing `_AccountAction`, `KEEP_ACCOUNT`, and `CLEAR_ACCOUNT` identities.
  `BaselineState` is exactly `UninitializedBaseline | ReadyBaseline | FailedBaseline`; `PromotionResult` exactly `Promoted | AlreadyInBand | NoLegacyRecord | PromotionFailed`; `ResolvedAccount` exactly `InBandAccount | LegacyAccount | NoAccount`.
- Session establishment precedes token use. Loaded source and baseline stay paired. Background work
  has explicit close/drain ownership. Dependency-bottom modules have no upward or lazy facade rejoin.
- Every later `storage.py` change is net-shrinking and lowers its exact LOC pin in the same diff.
  Compatibility lasts through v0.x; removal waits for an announced v1 runway.

The first account seam is the unused dependency-bottom `profile_account.py` leaf. It defines immutable `ProfileAccount`, closed keep/clear/set directives, `DomainSelection`, and `StoredSession`; the session composes the canonical immutable `CookieJar`.
Direct construction is permissive so a later compatibility adapter can preserve odd legacy values. Validation belongs to the named parsers:

| Parser input / view | Typed result |
|---|---|
| Non-mapping account, every view | `None` |
| Mapping, `ROUTE` | normalized account; invalid `authuser` becomes `0`, blank/invalid email becomes `None` |
| Mapping, `OWNER` | normalized account only when a non-blank string email is present |
| Mapping, `CARRY` | the same normalized account-record projection |
| Non-mapping domain namespace | empty `DomainSelection` |
| Mapping domain namespace | strings from a list become a defensive `frozenset`; optional is enabled only by exact `True` |

`CARRY` is not a lossless namespace carrier. Existing remint/capture operations may preserve the entire raw `notebooklm` namespace, including unknown keys; those operations must not round-trip it through `ProfileAccount`. No production caller consumes the new leaf in this stage.

The v0.x `AccountRecord`, `_AccountAction`, sentinels, `AccountArg`, writer signature/default, pickle path, facade/shim identities, and permissive direct construction remain owned by `storage.py`. The login compatibility boundary now applies this exact conversion table:

| Legacy runtime value | Internal directive |
|---|---|
| exact `KEEP_ACCOUNT` singleton | `KeepAccount()` |
| `AccountRecord` instance | `SetAccount(ProfileAccount(value.authuser, value.email))`, without normalization |
| everything else, including `CLEAR_ACCOUNT` and out-of-contract values | `ClearAccount()` |

`ProfileDocument` is the next dependency-bottom seam. `decode()` accepts only an object at
the root, then captures a recursively immutable, insertion-ordered snapshot without validating
nested shapes. `to_json()` always returns a new deep mutable tree. The lossless representation keeps
unknown root and namespace members, arbitrary origins, every cookie-list slot and duplicate, opaque
rows, unknown row fields, and scalar distinctions such as integer `-1` versus float `-1.0`.

Raw and typed views have separate jobs. `raw_cookie_rows()` requires an actual raw list and returns
defensive copies of all slots; missing or non-list cookies produce a bounded, value-free structural
error. `cookies()`, account views, and domain-selection views are intentionally tolerant, lossy
projections through the canonical cookie/account parsers. A typed view is never serialized back
into the raw document, and typed `CARRY` never substitutes for lossless namespace carry.

Copy-on-write operations preserve the original document and all unrelated raw members:

- cookie-row replacement eagerly copies an iterable without filtering or normalizing it;
- namespace replacement installs an exact copy, preserves `{}`, or removes the member for `None`;
- indexed row patches validate the fully materialized patch set before applying it, reject boolean
  or non-integer, out-of-range, and duplicate indices with bounded value-free errors, and allow
  mapping/opaque replacements in either direction.

This value chooses no corruption or operation policy. Missing files, filesystem and Unicode errors,
JSON text/syntax failures, malformed nested shapes, warnings/logging, backups, lock outcomes,
filtering, and whether an invalid raw cookie list is fatal remain decisions of the reader or
operation boundary. The first production consumers are the pure cookie decision leaf and its
`storage.py` transaction adapter; no reader, lock, I/O helper, facade, or lifecycle owner may import
the document value directly.

The first production owner extraction is `storage_lock.py`. `StorageLockManager` now owns the
process-default exact-raw-path thread-lock registry, concrete POSIX/Windows gateway, synchronous
bounded retry dependencies, and thread-safe cookie-warning claim. Direct construction creates an
isolated lifecycle. `storage.py` retains secure-parent and per-intent outcome policy plus its v0.x
`_file_lock` / `_file_lock_exclusive` seams; `keepalive.py` retains a separate local `_file_lock`
wrapper, and both route through the same process default. Full-writer white-box tests now replace
`storage._STORAGE_LOCKS`; cookie seam tests may still patch `storage._file_lock`. The old static
warning bool and `_acquire_storage_lock` helper are removed, and the exact storage pin falls from
3,102 to 2,829 lines in the same diff.

Cookie merge policy is the first algorithm extracted from the facade. `cookie_merge.py` owns the
pure snapshot/CAS decision and the permanent no-baseline overlay over `ProfileDocument`,
`CookieJar`, and `RecoveryObservation` values. It names dirty-tuple comparison (excluding
SameSite), value-only CAS, and exact-path dotted-domain equivalence independently; neither
`Cookie.__eq__` nor serialization chooses those policies. Decisions are immutable, redacted, and
carry a complete replacement document only when rows changed, plus the next baseline and rejected
identities. Unknown raw members survive ordinary changes; recovery replacement intentionally emits
one canonical winning row and drops that winner's unknown keys, preserving the established
contract.

In the pure-merge extraction stage, `storage.py` remained the sole transaction and compatibility
owner. It read and
classifies corruption under the existing blocking cookie lock, converts the legacy NamedTuple
snapshot/recovery inputs to immutable values, invokes the pure decision, reproduces the existing
value-free CAS logs, performs the single sanctioned raw write, and projects the old bool or
`CookieSaveResult`. The old tuple types, private helper signatures, same-module late binding, lock
semantics, writer authority, and caller identities do not move. This extraction lowers the exact
facade line pin again without changing bytes or baseline advancement behavior.

The commit spine and real store boundary now own cookie transactions, in-band account intents, and
all three profile replacements. `credential_io.py` is the sole importer of the unchecked atomic
JSON capability: one raw forwarder serves exactly `ProfileStore` for complete profiles and
`MasterTokenFile` for arbitrary token documents. `ProfileStore` owns one caller-spelled path, a
canonical ordering key, fresh document/session/account/derived-token reads, bounded-lock mechanics,
blocking cookie transactions, typed token/account writes, and remint, login/import, and
minted-session replacement. It owns no cache, baseline, live HTTP jar, scheduler, logger policy
object, or injectable writer.
Remint reads the latest destination under lock only when raw namespace carry is requested,
preserves the whole valid `notebooklm` mapping, filters an isolated source, and commits at most once.
Login/import replacement filters the isolated raw source, validates required cookie names before
any destination access, builds KEEP/SET/CLEAR namespaces with their intentionally distinct raw
preservation rules, optionally copies exact predecessor bytes to the sibling `.bak` while holding
the same lock, and commits once. Rejection logs only the missing count and raw path. SET isolates
the permissive account-field pair once when the request is built and again when the accepted payload
is projected under lock, using one shared deepcopy memo at each boundary.

The raw capture/domain policy and its value-free malformed-row diagnostics now live in the
dependency-bottom `cookie_filter.py` leaf. It retains whole allowed rows and their raw scalar types,
drops source origins and namespace data, and owns no path, document, lock, commit, or lifecycle
state. Browser capture, CLI import, all profile replacement writers, and compatibility shims keep
the same filter function identity through aliases.
Minted persistence snapshots the mutable live jar and runtime-permissive email before path or lock
work. Its repr-hidden request uses one shared-memo deepcopy and a manually built `CookieJar` with
the raw master-token serializer fields, including `same_site="None"`; it deliberately avoids the
filtering and SameSite-lossy `CookieJar.from_httpx()` constructor. Under the bounded lock,
`ProfileStore.replace_minted_session` reads the latest owner, applies the refusal/force matrix,
runs the default raw filter, preserves and rebinds the destination, and commits once. This pre-lock
immutable input snapshot is the intentional isolation correction for both jar and email.
Legacy migration and process lifecycle now live in `profile_migration.py`.
`LegacyAccountMigrator` owns the lossless raw compatibility projection, in-band/legacy/in-band
two-read resolution, typed sanitization, only-if-absent promotion, and embed-before-scrub order.
Its closed results are `InBandAccount | LegacyAccount | NoAccount` and
`Promoted | AlreadyInBand | NoLegacyRecord | PromotionFailed`. `LegacyAccountContext` alone owns
`context.json` read/scrub, its 10-second `FileLock`, public atomic write, and error/log behavior.
`LegacyPromotionScheduler` owns active paths, daemon workers/injection, and the shared-budget exit
drain warning (#2223); per-RPC reads never wait for the 90-second writer. Settled paths are retryable,
and an in-band winner with a stale legacy sibling schedules scrub-only reconciliation, so lock
failure and a stop between embed and scrub self-heal (#2228).
`LoginProfileWriter` reconciles only after `APPLIED` and lock release using the literal raw-key rule;
`AccountMetadataWriter` preserves write/clear-specific post-operation scrub and exception ordering.
At that stage, `storage.py` remained the v0.x signature/result facade. Exact pins were storage 1,127,
migration 311, store 814, filter 96, and token file 89 lines (2,425 combined).

Phase 9 lands the typed stored-auth boundary in `tokens.py`: raw-profile-bearing file and captured inline sources, `LoadPolicy(allow_headless)`, paired seeds/acquisitions, final-attempt route resolution, the closed `LoadedAuth` union, and concrete `SessionSeedLoader`, `AccountRouteResolver`, and `StoredAuthLoader` around the sole structural port, `TokenAcquirer`.
Cookie load and every refresh/recovery replacement produce one live jar plus its exact SameSite-preserving typed baseline; the initial merge advances accepted final identities, retains rejected old identities, and keeps the acquisition baseline after hard failure.
Phase 10 makes `CookiePersistence._from_store` the first-party runtime owner: `FileLoadedAuth` registers its exact store/baseline without a reread, while direct construction prepares one disk baseline before transport and a fileless client captures only a live compatibility projection.
Per-path `Uninitialized | ReadyBaseline | FailedBaseline` state is isolated from `_LegacySnapshotAdapter`; canonical saves are ordered typed store merges. The default route is unconditional and never inspects the public storage wrapper's identity. Only an explicit `cookie_saver=` uses `_save_v0_callback`; a non-default override lazily initializes its own retryable snapshot. `ClientLifecycle` alone mirrors the loaded projection into its client-owned `AuthTokens` after open and accepted saves; `_from_store` retains no `AuthTokens`.
Phase 10B makes native store results load-bearing. Browser capture uses `RemintWriteRequest` and
`ReplaceResult`; app/CLI callers use `replace_profile_from_login` with primitive account modes.
Legacy results survive only in compatibility owners and the explicit callback adapter; exhaustive
maps and guardrails pin every projection, exception, native caller, and named behavior test.
The resulting graph is 41 modules / 15,898 lines / 142 edges (130 module + 12 local), with no SCC.
Phase 11B makes `MasterTokenFile` the one-read raw/typed file owner and sole token-commit caller; 11C moves exchange/mint and the raw RotateCookies wire into `MintService`; 11D moves bootstrap/re-mint/missing-storage policy into `MasterTokenBootstrapper`. Phase 12A introduces the one-shot cold coordinator; 12B gives refresh one paired live/SameSite baseline and one typed store merge. Phase 12C completes ownership: `SingleFlight`, `ColdRecoveryState`, and `RotationState` own process state; the coordinator owns the sole L2.5/L3/L4 bodies; PSIDTS recovery uses injected pure loaders, typed document/CAS, and `ProfileStore`; dependency-bottom values own `MasterTokenError`, `Account`, and the repair result; `AccountRepairService` owns one repair. The graph is 40 modules / 15,237 lines / 128 edges (117 module + 11 local), with both SCC sets empty. Public savers, facade values/errors, module adapters, raw keepalive views, logs, traceback/error identity, cancellation, and runtime injection seams remain compatible. Phase 13D preserves that v0.x behavior while announcing the v1 removal of the two `AuthTokens` entry points that independently own storage loading; immutable specs and an import-free checker keep their warning text, targets, and callsites synchronized. Phase B0c later generalizes only the OAuth step behind frozen `OAuthClientSpec` and `MintedOAuthToken` values: the web adapter supplies its unchanged Chromecast identity, optional server expiry remains unknown when absent or malformed, and the source guard pins one production `perform_oauth` call.

The compatibility inventory is explicit:

| Documented late-bound source seam | Focused characterization node | Category | Removal decision / milestone |
|---|---|---|---|
| `_auth.refresh.RefreshCmdDeps.runner`, `.flock`, `.lock_path` (`_run_refresh_cmd`, `_keepalive._file_lock_try_exclusive`, `_auth_paths._refresh_lock_path`) | `tests/unit/test_refresh_lock_registry.py::TestFlockLoserWaitsThenReloads::test_leader_body_loser_waits_no_subprocess_no_bump` | `compat_adapter` | Retain through v0.x; remove only when the refresh-command facade and its partial-dependency default are retired at v1. |
| `_auth.account._extract_active_email_for_repair` | `tests/unit/test_auth_account_repair_service.py::test_extractor_runs_before_handled_region_and_still_scrubs` | `compat_adapter` | Retain until the network adapter constructs `AccountRepairService` without the v0.x account wrapper; no earlier capture-at-import rewrite. |
| `_auth.cookies.build_httpx_cookies_from_storage`, `_build_httpx_cookies_from_storage_strict` → `_auth.psidts_recovery.load_with_recovery` | `tests/unit/test_auth_load_composition.py::TestBothWrappersUseTheOneComposition::test_jar_wrapper_delegates`, `tests/unit/test_auth_load_composition.py::TestBothWrappersUseTheOneComposition::test_passive_jar_loader_selects_name_only` | `compat_adapter` | Remove with the two legacy storage-loading entry points in the announced next-major `AuthTokens` retirement. |
| `_auth.psidts_recovery.validate_with_recovery` → `validate` / `heal`, plus `recover_psidts_in_memory` | `tests/unit/test_auth_validate_heal_split.py::TestWrapperComposition::test_post_heal_recheck_is_presence_only` | `compat_adapter` | Retain while v0.x CLI browser extraction calls the wrapper; delete wrapper and characterization together at v1. |
| `_auth.keepalive._STORAGE_LOCKS`, `_file_lock`, `_rotation_lock_path` and the public-facade identity views `_POKE_STATE_LOCK`, `_POKE_LOCKS_BY_LOOP`, `_LAST_POKE_ATTEMPT_MONOTONIC` | `tests/unit/test_auth_storage_lock.py::test_process_default_and_module_ownership_are_identity_stable`, `tests/unit/test_auth_keepalive.py::TestPokeConcurrencyThrottling::test_rotation_lock_path_is_sibling_of_storage` | `compat_adapter` | Raw state views remain only for v0.x compatibility and are removed with the next-major facade; tests must use `RotationState`/`StorageLockManager` afterward. |
| `_auth.master_token.generate_android_id` and `_auth.cookies._build_httpx_cookies_from_storage_strict` as call-time `MasterTokenBootstrapper` bridges | `tests/unit/test_auth_master_token_bootstrap.py::test_oauth_call_time_bridges_observe_midflight_rebinding`, `tests/unit/test_auth_master_token_bootstrap.py::test_strict_loader_bridge_observes_midflight_rebinding` | `compat_adapter` | Retain through v0.x master-token wrappers; inject the concrete service directly after their v1 removal. |
| Public `notebooklm.auth` storage/account/mint/token wrappers and result/error identities | `tests/unit/test_auth_storage.py::test_load_auth_from_storage_lives_in_private_module`, `tests/unit/test_auth_master_token_bootstrap.py::test_public_oauth_exchange_base_exception_traceback_does_not_retain_oauth_token` | `compat_adapter` | Preserve names, signatures, result/error identity, and call-time canonical-home lookup through v0.x; remove only in the announced v1 facade retirement. |
| Browser runtime facade (`_BrowserFacadeIO`, `_browser_*` call-time loaders) | `tests/unit/test_auth_browser_facade.py::test_capture_delegates_plan_and_projects_page_html`, `tests/unit/test_auth_browser_facade.py::test_oauth_capture_delegates_and_scrubs_facade_frame` | `compat_adapter` | Retain until the public auth facade stops owning browser-runtime dispatch at v1; concrete browser owner tests remain. |

These rows are the complete exception list for documented call-time lookup. A new source comment declaring another patch/lookup seam must add its exact row and characterization node here in the same change; underscore naming alone is not an exception. Owner behavior uses extracted services directly; remaining adapter probes are frozen by full joint-row policy until the stated v1 retirement or owner migration.

- Profile writers: `merge_cookie_delta`, `update_account_metadata`, `clear_in_band_account`, `replace_from_remint`, `replace_from_login`, `persist_minted_jar`; arbitrary: `write_master_token`.
- Cookie adapters: public `save_cookies_to_storage`, snapshot helpers, and compatibility
  constructors remain; `_save_v0_callback` isolates explicit savers. Module-identity fallback is retired.
- Client/token seams: `NotebookLMClient.__init__(cookie_saver=...)`, `AuthTokens.__init__`, `AuthTokens.from_storage`.
- Ladder facade: `load_auth_from_storage`, `fetch_tokens`, `fetch_tokens_passive`, `fetch_tokens_with_domains`, `validate_with_recovery`, `recover_psidts_in_memory`.
- Account facade: account assertions, metadata read/write/clear, route/email lookup, legacy scrub,
  Playwright repair, and identity resolution remain compatible.
- Mint/token facade: exchange, mint, persist, read/write, bootstrap, and re-mint operations remain.

The frozen read policy remains per intent: cookie merge hard-fails non-raising; account update is
fail-closed while clear is best effort; remint replaces malformed destinations; login backs up exact
bytes without parsing the destination; minted sessions enforce their owner gate; master-token reads
wrap I/O/JSON failures and reject malformed records. Tests pin results, errors, logs, bytes, and writes.

## Consequences

`ProfileStore` becomes the real aggregate and `storage.py` a shrinking facade without creating an
`_auth/storage/` package. Raw and canonical paths have visibly separate jobs. Cookie merge stays a
pure decision plus one commit; lifecycle state gains owners; secret-bearing baselines do not cross a
host extension protocol. Only `TokenAcquirer` is structural: ports appear with consumers, not ahead
of them.

The cost is a long compatibility migration. Old tuples, enum constants, transaction shims, direct
token paths, saver injection, loader fallback, verifier bridge, and intentionally odd corruption
behavior remain until their runway ends. Every stage carries exact writer, signature, patch,
module-size, transaction, and import-graph evidence, making temporary duplication visible.

## Alternatives considered

**Mechanical file split or a class inside `storage.py`.** Rejected: both retain shared globals and
upward imports; the latter also cannot grow under the exact pin.

**`_auth/storage/` package or generic repository/unit-of-work.** Rejected: one aggregate does not
justify another facade or abstraction family.

**Universal mutation/corruption policy.** Rejected: the frozen intents deliberately disagree.

**Immutable transport jar or equality-driven cookie merge.** Rejected: httpx mutates its jar;
SameSite is serialized but excluded from equality, and merge predicates are independently named.

**Delete process state, promotion scheduling, or its exit drain.** Rejected: shared lock identity,
recovery flights, non-blocking per-RPC reads, and short-process durability are real lifecycles.

**Alias old tuple/directive values or fake a profile for arbitrary token paths.** Rejected: runtime
identity and explicit-path behavior are observable v0.x contracts.
