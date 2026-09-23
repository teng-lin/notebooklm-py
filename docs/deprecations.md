# Deprecations

**Status:** Active
**Last Updated:** 2026-09-06

This page is the **single source of truth** for currently-deprecated APIs in
`notebooklm-py`. Each row lists what is deprecated, the recommended
replacement, when the deprecation was introduced, when it is scheduled for
removal, and any cross-references.

The [release migration gates](#release-migration-gates) below record
whether each scheduled transition has actually shipped its notice and reached
its earliest eligible release. A `Since` value staged on the development branch
is not release evidence.

`docs/stability.md` links here rather than duplicating the table; if you need
the broader stability policy (semver promise, supported Python versions, the
0.x pre-1.0 semantics), start there.

> **Upgrading to v0.8.0?** The breaking error-and-return contract changes
> **shipped in v0.8.0**. See the consolidated
> [Upgrading to v0.8.0](upgrading-to-0.8.0.md) guide for the full set and the
> exact before→after migration for each. The `NOTEBOOKLM_FUTURE_ERRORS` preview
> flag that staged these changes in v0.7.0 has been **removed** (it is now a
> no-op).

## Scheduled for removal

| Deprecated | Replacement | Since | Removal | Notes |
|------------|-------------|-------|---------|-------|
| Raw prefetch keywords (`artifacts_data`, `artifacts`, `mind_maps`) on per-kind artifact downloads | `artifacts.prepare_downloads(request)` then `artifacts.download(selection, path)` | v0.9.0 (planned; unshipped) | v1.0 only if its own notice interval is met; otherwise a later breaking release | Registered key `artifact_raw_download_prefetch`. Existing signatures and raw-prefetch behavior remain supported; only supplied non-`None` raw values warn. See [download contracts](python-api.md#prepared-artifact-downloads). |
| Web interactive mind-map hydration after a waited failed/removed completion | `client.mind_maps.generate(..., failure_policy="raise")` | Source registration v0.9.0; **not yet shipped** | Earliest target v1.0, conditional on its own stable warning release and migration interval | Exact key `mind_map_legacy_terminal_hydration`; warns only when legacy hydration actually continues. The default remains `"legacy"`; Android already raises in legacy mode. Completed, non-waited, note-backed, and explicit strict calls stay silent. See C5A-01 in the release migration gates; postpone the default flip if that independent gate is unmet. |
| Non-default flat tuning arguments on `NotebookLMClient(...)` | `config=ClientConfig(...)` with owner-specific groups from `notebooklm.options` | v0.9.0 | v1.0 | One caller-attributed warning names all non-default legacy tuning arguments. Explicit values equal to the historical defaults remain silent because the 0.x signature cannot distinguish them from omission. Credential inputs remain separate. |
| Non-default flat tuning arguments on `NotebookLMClient.from_storage(...)` | `config=ClientConfig(...)` with owner-specific groups from `notebooklm.options` | v0.9.0 | v1.0 | One warning is emitted at the `from_storage(...)` call before deferred auth loading. `path`, `profile`, and `allow_headless` remain credential-loader inputs. |
| Names or partial IDs on a confirmed MCP mutation | Re-submit the canonical `notebook_id` and target ID(s) returned by the `needs_confirmation` preview | v0.9.0 | v1.0 | During v0.9, successful confirming calls still resolve legacy names but emit one registered `DeprecationWarning` and add a `deprecation` key to that confirming result. Preview calls remain name-friendly. `NOTEBOOKLM_MCP_STRICT_IDS=1` already rejects names before any list or mutation call. |
| `NotebookLMClient.rpc_call(...)` | Web: `client.raw.call(...)`; Android: `client.raw.unary(...)` / `unary_stream(...)`, or a separate Web-selected client's `raw.call(...)` | v0.9.0 | v1.0 | Warns once per client at the call boundary. During v0.x, an Android client preserves the historical Web call by lazily materialising a Web compatibility sidecar; the sidecar requires Web cookies and never starts keepalive. |
| `max_not_found` / `min_not_found_window` on `ArtifactsAPI.wait_for_completion(...)` | Use `timeout` to bound unresolved listing absence | v0.9.0 | v1.0 | Accepted but ignored after #2432: absence never synthesizes removal or quota failure. Non-default values warn once at the caller; values equal to historical defaults (`5`, `10.0`) remain silent because the signature cannot distinguish them from omission. The `REMOVED` enum remains available for legacy statuses. |
| Differing follower options for `ArtifactsAPI.wait_for_completion(...)` | Pass the same effective polling options as the active leader during v0.x; v1.0 applies each waiter's own options | v0.9.0 | v1.0 | The first waiter still owns `timeout` and interval options during v0.x. A follower whose effective value differs emits one warning naming the ignored values. |
| Follower `on_status_change` callback cardinality | Treat a follower callback as final-status-only during v0.x; v1.0 delivers every observed status to each subscriber | v0.9.0 | v1.0 | A non-`None` follower callback emits a separate warning because its delivery cardinality changes in v1.0. |
| `AuthTokens.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` then use `client.auth` inside the managed lifecycle | v0.8.1 | v1.0 | The compatibility loader keeps its signature, return, error, and cancellation behavior through v0.x but now emits `DeprecationWarning` when awaited. |
| `AuthTokens(..., storage_path=..., cookie_jar=None)` synchronous storage fallback | Use `NotebookLMClient.from_storage(...)`, or supply `cookie_jar=` when constructing tokens directly | v0.8.1 | v1.0 | Only the implicit synchronous-I/O branch warns; construction without `storage_path`, with a supplied jar, or failing cookie normalization stays silent. |
| `AuthTokens.flat_cookies` | `AuthTokens.jar` for bootstrap-cookie questions; managed `NotebookLMClient` request APIs for HTTP | v0.8.1 | v1.0 | Direct property access emits one caller-attributed `DeprecationWarning`. It is a lossy name-only projection and cannot preserve domain/path siblings. `NOTEBOOKLM_QUIET_DEPRECATIONS=1` suppresses the warning. |
| `AuthTokens.replace_cookie_jar(...)` | Managed `NotebookLMClient` request APIs | v0.9.0 | v1.0 | Direct calls emit a caller-attributed `DeprecationWarning`. Jar replacement is an internal compatibility sync-back operation; first-party refresh and recovery use its warning-free private counterpart. |
| `AuthTokens.cookies` / `AuthTokens.cookie_jar` | Use `AuthTokens.jar` as the v0.x migration shape; adopt the immutable `initial_cookies: CookieJar` bootstrap field in v1 | v0.8.1 | v1.0 | **Docs-only deprecation:** these remain dataclass fields through v0.x, so runtime warnings would leak through construction, repr, equality, and `dataclasses.replace()`. They are public compatibility shadows, not the managed client's live jar. |
| `AuthTokens.cookie_snapshot` | No public replacement; managed client persistence owns save baselines | v0.9.0 | v1.0 | **Docs-only deprecation:** this internal save baseline remains a dataclass field through v0.x. Access cannot warn without leaking warnings through generated dataclass operations. |
| `AuthTokens.jar` | The v1 `AuthTokens.initial_cookies` bootstrap field | v0.8.1 | v1.0 | Warning-free v0.x migration shape. It is an immutable question/input projection, not a second live-cookie authority. |
| `AuthTokens.cookie_header` | Managed `NotebookLMClient` request APIs | v0.8.1 | v1.0 | Docs-only deprecation. Its name-only, domain-blind join is unsafe for request construction; it remains warning-free through v0.x so it does not indirectly trigger the `flat_cookies` warning. |
| `AuthTokens.cookie_header_for(url)` | Managed `NotebookLMClient` request APIs | v0.8.1 | v1.0 | Docs-only deprecation. Domain-aware selection remains compatible for standalone callers, but first-party request paths already use the kernel-owned jar. |
| `Artifact.from_api_response(...)` / `Artifact.from_mind_map(...)` | `client.artifacts` typed APIs | v0.9.0 | v1.0 | Direct calls emit caller-attributed `DeprecationWarning`. These factories expose private Web response rows; there is no supported public raw-row decoder. |
| Artifact `get()` / `get_or_none()` projecting an incomplete aggregate no-hit as absence | `client.artifacts.lookup(...)`, inspect `ArtifactLookup.status` for `MISSING` versus `UNKNOWN` | v0.9.0 (planned; not shipped at the 2026-09-06 audit) | v1.0, only after the required shipped runway | The registered `artifact_ambiguous_absence` warning is targeted: complete misses and all positive hits stay warning-free; it fires only when note-backed mind maps are unavailable and the legacy API preserves `ArtifactNotFoundError` / `None`. |
| Android `get_prompt(..., require_complete=False)` projecting an incomplete aggregate no-hit as absence | `get_prompt(..., require_complete=True)` | v0.9.0 (planned; not shipped at the 2026-09-06 audit) | v1.0, only after the required shipped runway | The default remains `False` in 0.x. Its ambiguous Android path emits the same registered `artifact_ambiguous_absence` warning. Web already uses a direct strict decoder path and does not add a preliminary aggregate lookup. |
| `Collection.from_api_response(...)` | `client.collections` typed APIs | v0.9.0 | v1.0 | Direct calls emit caller-attributed `DeprecationWarning`. Typed Web operations decode internally without warning. |
| `Label.from_api_response(...)` | `client.labels` typed APIs | v0.9.0 | v1.0 | Direct calls emit caller-attributed `DeprecationWarning`. Typed Web operations decode internally without warning. |
| `Notebook.from_api_response(...)` | `client.notebooks` typed APIs | v0.9.0 | v1.0 | Direct calls emit caller-attributed `DeprecationWarning`. Typed Web operations decode internally without warning. |
| `ShareStatus.from_api_response(...)` / `SharedUser.from_api_response(...)` | `client.sharing` typed APIs | v0.9.0 | v1.0 | Direct calls emit caller-attributed `DeprecationWarning`. Typed Web operations decode internally without warning. |
| `Source.from_api_response(...)` / `Source.from_row(...)` | `client.sources` typed APIs | v0.9.0 | v1.0 | Direct calls emit caller-attributed `DeprecationWarning`. `SourceRow` is a private Web adapter and there is no supported public raw-row decoder. |
| Awaiting `NotebookLMClient.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` | v0.5.0 | v1.0 | The `__await__` form still works. Warning emitted via `src/notebooklm/_deprecation.py::warn_deprecated`; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1` ([#1369](https://github.com/teng-lin/notebooklm-py/issues/1369)) |
| MCP `research_status(task_id=…)` / `research_import(task_id=…)` / `research_cancel(run_id=…)` | The same value under `poll_task_id=…` on all three | v0.8.0 | v0.9.0 | The three tools each accept the id that `research_start` / `research_status` surface as `poll_task_id` — renamed so the value copies verbatim between tools. The old `task_id` / `run_id` param names still work as aliases but emit a `DeprecationWarning` (via `warn_deprecated`) and add a `deprecation` note to the tool result; passing both names with different values is a validation error. ([#1789](https://github.com/teng-lin/notebooklm-py/issues/1789)) |
| Pre-profiles home-root layout (`~/.notebooklm/storage_state.json`, `context.json`, `browser_profile/` read directly at the home root, outside `profiles/<name>/`) | `profiles/<name>/…` — run any `notebooklm` command once to migrate automatically | v0.8.1 | v1.0 | Only reached when the profile-dir path doesn't exist AND the resolved profile is `"default"` (`paths.py::_legacy_fallback`); one `notebooklm` invocation triggers `migrate_to_profiles()` and the fallback is never hit again. Emits a `DeprecationWarning` (via `warn_deprecated`) on each read; suppress with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. ([#2103](https://github.com/teng-lin/notebooklm-py/issues/2103)) |
| `ChatReference.answer_start_char` / `answer_end_char` (dataclass fields) | `ChatReference.fragment_start_char` / `fragment_end_char` | v0.8.1 | v1.0 | The names claimed an **answer-text** position; the slot is the cited fragment's **source-side** range (wire `Citation` tag 4 — the union of the fragment's own element ranges, in the same coordinate space as `start_char`/`end_char`). A live capture returned `[1130, 1695]` for the third citation of a **536-character** answer, so anyone who followed the old docstring and sliced the answer with it indexed far off the end. **Docs-only deprecation**, for the same reason as `Notebook.modified_at` below: these are dataclass **fields**, so a runtime warning on access would also fire from `repr()` / `__eq__` / `dataclasses.replace()` and the MCP/REST `to_jsonable` serializer, flooding callers who never typed the old name. `__setattr__` keeps the pair in lock-step in both directions and `__post_init__` seeds the canonical fields from the legacy keywords, with the canonical name authoritative on a disagreement — so reads, keyword construction, positional construction, unpickling (`__setstate__`) and the serialized JSON keys all keep working, and the old names keep returning exactly the values they always did. Two exceptions, the same pair `Notebook.modified_at` carries below: the mirror fires only on **non-`None`** assignment, so clearing one name post-construction leaves the other stale; and `dataclasses.replace(ref, answer_start_char=X)` is a no-op on a reference that already has a `fragment_start_char` — pass `fragment_start_char=X` instead. The genuinely answer-side value is the **new** `answer_anchor_start` / `answer_anchor_end` pair, read from the answer document's annotation map and resolved with `AskResult.answer_document.slice(...)`, not against `AskResult.answer`. It is deliberately *not* named `answer_*_char`: it is a different coordinate space, not this field's successor, and a name that invited that substitution would reintroduce the very bug being fixed. ([#2120](https://github.com/teng-lin/notebooklm-py/issues/2120)) |
| `Notebook.modified_at` (dataclass field) | `Notebook.last_viewed_at` | v0.8.1 | v1.0 | The wire slot is `lastViewedTime`, **not** a modification time: it advances when this account merely *reads* the notebook and does not move when a collaborator edits it. **Docs-only deprecation:** `modified_at` remains a dataclass field through v0.x — a runtime warning on field access would also fire from `repr()`, `__eq__`, `dataclasses.replace()` and the MCP/REST `to_jsonable` serializer, flooding callers who never typed the old name (the same reasoning as `AuthTokens.cookies` / `cookie_jar` above). `__setattr__` keeps the two in lock-step (including on the in-place timestamp backfill) and `__post_init__` seeds the canonical field from a legacy `modified_at=` keyword, with `last_viewed_at` authoritative — so reads, keyword construction, positional construction and the serialized `modified_at` JSON key all keep working. One exception: `dataclasses.replace(nb, modified_at=X)` is a no-op on a notebook that already has a `last_viewed_at` — pass `last_viewed_at=X` instead. ([#2126](https://github.com/teng-lin/notebooklm-py/issues/2126)) |
| `NotebookMetadata.modified_at` (property) | `NotebookMetadata.last_viewed_at` | v0.8.1 | v1.0 | Same rename, same reason. This one **is** a property rather than a dataclass field, so it can warn at exactly the boundary ADR-0018 asks for: attribute access emits one caller-attributed `DeprecationWarning` (via `warn_deprecated`), suppressible with `NOTEBOOKLM_QUIET_DEPRECATIONS=1`. `NotebookMetadata.to_dict()` reads `last_viewed_at` internally and emits **both** keys, so serializing never warns and no consumer of the old key breaks. ([#2126](https://github.com/teng-lin/notebooklm-py/issues/2126)) |

`CookieJar` remains an immutable, ordered sequence of `Cookie` rows. It preserves
full-fidelity rows when constructed from authoritative row data;
`CookieJar.from_httpx()` is SameSite-lossy and is only a transient live
observation. It is never a `Mapping[str, str]` and never the managed client's
live mutable jar. Iteration yields rows, `len()` counts rows, and domain/path
siblings remain distinct; the deprecation runway does not change those semantics.

> The v0.8.0 error-contract runways (`get()`-returns-`None`, the
> `wait_for_completion(interval=...)` alias, the dict-subscript bridge,
> `NotebooksAPI.share()`, and the ambiguous `research.poll(task_id=None)` guard)
> all completed their removal cycle in **v0.8.0** — see
> [Removed in v0.8.0](#removed-in-v080) below.

## Release migration gates

**Audited:** 2026-09-06
**Source baseline:** `f31f0f9d1db225242ac8f7754f955444b0fcff46`

This inventory separates a migration's design state from its release eligibility. An accepted ADR,
source warning, test registry row, changelog draft, or version comment proves design or runway
preparation only. Eligibility requires the notice or preview to exist in an actual published stable
release, followed by the interval required by [stability policy](stability.md#deprecation-policy).
Each transition owns its own evidence; rows cannot borrow another migration's release date.

The existing v1 inventory below is an exact one-for-one ledger of all 34 entries in
`tests/_guardrails/_v100_breaks.py`. Keep that registry and this table complete until the release
cut drains both deliberately. The additional credential and C3/C4/C5 rows record migrations the
v1 registry did not yet express at the audit baseline.

### Published release evidence

The audit used the GitHub release list, remote tag refs, and the files served from those tag refs.

| Release | Published evidence | Source evidence relevant here |
| --- | --- | --- |
| v0.5.0 | [GitHub release, 2026-05-24](https://github.com/teng-lin/notebooklm-py/releases/tag/v0.5.0), commit `7621088c03a804fbdf4c8b5959bd9a9faafcc4c6` | The changelog and `client.py` ship the warning for awaiting `NotebookLMClient.from_storage(...)`. |
| v0.8.1 | [GitHub release, 2026-08-14](https://github.com/teng-lin/notebooklm-py/releases/tag/v0.8.1), remote tag `01c419a0474e0191b88e94c572d605b4899a9c2b` | Tagged `_deprecation.py` ships the three registered `AuthTokens` warnings; tagged deprecation docs ship the Web cookie-field, home-root, citation, and notebook-name runways. |
| v0.8.2 | [GitHub release, 2026-09-02](https://github.com/teng-lin/notebooklm-py/releases/tag/v0.8.2), remote tag `c1008a4416e338b7497a7db7db0500fad5f097e6` | Latest stable release at audit time. It continues the v0.8.1 runways but predates the P5/P6/P7 warning work merged later. |
| v0.9.0 | **No GitHub release and no remote tag at audit time.** | Every current row whose registry or docs says `Since = 0.9.0` is unshipped. Source text does not satisfy its gate. |

For a migration first shipped in v0.8.1 or earlier and scheduled for the next major, the earliest
eligible release is v1.0.0. For a staged v0.9.0 migration, v1.0.0 is only conditionally earliest:
a stable v0.9.0 containing that exact warning must publish first, and C9b cannot be the same release
that first introduces the warning. New 0.x transitions retain their compatibility paths until their
own stable notice release and required interval have elapsed, even if that is later than v1.0.0.

### C9 credential-surface decisions outside the original registry

[ADR-0039](adr/0039-backend-specific-credential-surfaces.md) settles the destination. These rows
remain release-ineligible because the 0.x surface does not yet warn users about the corresponding
Android/client-auth changes.

| ID | Transition | Owner | Class | First shipped notice | Earliest eligible transition | Gate and guardrail disposition |
| --- | --- | --- | --- | --- | --- | --- |
| C9-CRED-01 | Direct Android construction changes from Web `AuthTokens` to public `AndroidMasterToken`; mismatches fail before I/O. | C9 | API + behavior | None | After its own stable notice release and interval | **Not met.** Add a registered constructor migration and API signature allowances only with the actual cut. |
| C9-CRED-02 | `client.auth` becomes `AuthTokens` on Web and a secret-free `AndroidAuth` view on Android; mutable identity semantics end. | C9 | API + behavior | None | After its own stable notice release and interval | **Not met.** Update public typing/baselines and supersede ADR-0016's auth-instance rule at the cut. |
| C9-CRED-03 | Android `get_account_authuser()` becomes unsupported; account email comes from the master-token identity. | C9 | Behavior | None | After its own stable notice release and interval | **Not met.** Behavioral runway and both-backend identity tests; API allowance only if the audited signature changes. |
| C9-CRED-04 | `refresh_auth()` becomes side-effect-only (`None`) and Android no longer refreshes a Web sidecar. | C9 | API + behavior | None | After its own stable notice release and interval | **Not met.** Registered return-contract warning, public signature/baseline update, and behavior tests. |
| C9-CRED-05 | `from_storage()` selects its loader before acquisition; Android ignores inline Web auth and requires no cookie file/homepage request. | C9 | Behavior | None | After its own stable notice release and interval | **Not met.** Backend/path/profile/inline-env matrix in the behavioral runway. The home-root removal remains separately tracked as P8-30. |

### Existing P8/v1 registry: complete 34-row inventory

Guardrail abbreviations used in the table:

- **API**: exact `scripts/api-compat-allowlist.json` entry for each reported break, retained through
  release and checked with `audit_public_api_compat.py --check-stale`.
- **REG**: registered `_deprecation.DEPRECATION_SPECS` warning and
  `scripts/check_deprecation_targets.py`.
- **DOC**: exact first-cell docs runway verified by the v1 release gate.
- **BEH**: v1 behavioral-runway entry and focused behavior tests; no API allowance when the audit
  reports no structural break.

| ID | Exact v1 registry key and transition | Owner | Class | Runway | First shipped notice | Earliest eligible | C9b release gate / guardrail |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P8-01 | `client_legacy_constructor_options` — remove flat client tuning keywords | P5/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-02 | `client_legacy_from_storage_options` — remove flat stored-client tuning keywords | P5/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-03 | `client_rpc_call_web` — remove Web root `rpc_call()` | P7/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-04 | `client_rpc_call_android` — remove Android-to-Web root `rpc_call()` | P7/C9 | API + behavior | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-05 | `artifact_poll_follower_options` — make polling options per waiter | P6/C9 | Behavior | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; REG + BEH |
| P8-06 | `artifact_poll_follower_callback` — deliver every observed status to followers | P6/C9 | Behavior | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; REG + BEH |
| P8-07 | `` `NotebookLMClient.rpc_call(...)`::Remove Android LazyWebSidecar `` | P7/C9 | Behavior/internal graph | `LazyWebSidecar` compatibility marker, dependent on P8-04 notice | None | Conditional v1.0.0 after stable v0.9.0 | **Not met**; BEH and clean-import isolation; no stale API allowance for the private class |
| P8-08 | `auth_tokens_from_storage` — remove `AuthTokens.from_storage()` | Auth/C9 | API | REG | v0.8.1 | v1.0.0 | **Notice met**; API + REG |
| P8-09 | `auth_tokens_sync_storage_construction` — remove synchronous storage fallback | Auth/C9 | Behavior | REG | v0.8.1 | v1.0.0 | **Notice met**; REG + BEH only |
| P8-10 | `auth_tokens_flat_cookies` — remove `AuthTokens.flat_cookies` | Auth/C9 | API | REG | v0.8.1 | v1.0.0 | **Notice met**; API + REG |
| P8-11 | `auth_tokens_replace_cookie_jar` — remove `replace_cookie_jar()` | Auth/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-12 | `` `AuthTokens.cookies` / `AuthTokens.cookie_jar`::Remove AuthTokens.cookies `` | Auth/C9 | API field | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-13 | `` `AuthTokens.cookies` / `AuthTokens.cookie_jar`::Remove AuthTokens.cookie_jar `` | Auth/C9 | API field | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-14 | `` `AuthTokens.cookies` / `AuthTokens.cookie_jar`::Change AuthTokens class shape `` | Auth/C9 | API signature | DOC | v0.8.1 | v1.0.0 | **Notice met**; separate class-signature API allowance + DOC |
| P8-15 | `` `AuthTokens.cookie_snapshot`::Remove AuthTokens.cookie_snapshot `` | Auth/C9 | API field | DOC | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + DOC |
| P8-16 | `` `AuthTokens.jar`::Remove AuthTokens.jar `` | Auth/C9 | API property | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-17 | `` `AuthTokens.cookie_header`::Remove AuthTokens.cookie_header `` | Auth/C9 | API property | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-18 | `` `AuthTokens.cookie_header_for(url)`::Remove AuthTokens.cookie_header_for `` | Auth/C9 | API method | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-19 | `artifact_from_api_response` — remove `Artifact.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-20 | `artifact_from_mind_map` — remove `Artifact.from_mind_map()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-21 | `collection_from_api_response` — remove `Collection.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-22 | `label_from_api_response` — remove `Label.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-23 | `notebook_from_api_response` — remove `Notebook.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-24 | `share_status_from_api_response` — remove `ShareStatus.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-25 | `shared_user_from_api_response` — remove `SharedUser.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-26 | `source_from_api_response` — remove `Source.from_api_response()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-27 | `source_from_row` — remove `Source.from_row()` | Types/C9 | API | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; API + REG |
| P8-28 | `mcp_confirmed_name_references` — reject names/partial IDs on confirmed mutations | P7/C9 | Behavior | REG | None (`0.9.0` is source-only) | Conditional v1.0.0 after stable v0.9.0 | **Not met**; REG + BEH |
| P8-29 | `` Awaiting `NotebookLMClient.from_storage(...)`::Remove awaitable factory path `` | Client/C9 | Behavior | Inline gated warning | v0.5.0 | v1.0.0 | **Notice met**; BEH only; the private wrapper produces no API-audit break |
| P8-30 | `Pre-profiles home-root layout::Remove home-root credential fallback` | Paths/C9 | Behavior | Inline gated warning | v0.8.1 | v1.0.0 | **Notice met**; BEH only; no API allowance |
| P8-31 | `` `ChatReference.answer_start_char` / `answer_end_char` (dataclass fields)::Remove answer_start_char `` | Types/C9 | API field | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-32 | `` `ChatReference.answer_start_char` / `answer_end_char` (dataclass fields)::Remove answer_end_char `` | Types/C9 | API field | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-33 | `` `Notebook.modified_at` (dataclass field)::Remove Notebook.modified_at `` | Types/C9 | API field | DOC | v0.8.1 | v1.0.0 | **Notice met**; API + DOC |
| P8-34 | `` `NotebookMetadata.modified_at` (property)::Remove NotebookMetadata.modified_at `` | Types/C9 | API property | Inline gated warning + docs | v0.8.1 | v1.0.0 | **Notice met**; API + BEH |

The P8 API allowance must also cover every removed constructor/from-storage keyword and every
public compatibility export that the audit actually reports. Conversely, P8-07, P8-09, P8-29, and
P8-30 are behavioral/private shapes and must not receive invented allowances: `--check-stale`
rejects entries with no matching `ApiBreak`. The v0.8.0 release-gate pins remain historical and
must not be repopulated.

### Independent C3/C4/C5 migration rows

These migrations were designed after the original P8 inventory. None has a shipped notice at the
audit baseline, so none is authorized merely because a v1 release occurs. Implementing phases must
replace “pending exact key” with the literal registry key and first shipped version.

| ID | Transition | Owner | Class | First shipped notice | Earliest eligible | Gate and disposition |
| --- | --- | --- | --- | --- | --- | --- |
| C3-01 | Legacy artifact `get()`/`get_or_none()` no-hit behavior changes from best-effort aggregate absence to authoritative `MISSING`/`UNKNOWN`. | C3 | Behavior; signatures remain unchanged | None; source registers `artifact_ambiguous_absence` for the next stable release | After that warning ships and its required interval | `artifact_ambiguous_absence` fires only when incomplete backing would become false absence; additive `list_with_status()`/`lookup()` and the both-backend completeness matrix are present. Do not count the source registration as shipped. |
| C3-02 | Android `get_prompt(..., require_complete=False)` default becomes strict/authoritative. | C3 | API default + behavior | None; source registers `artifact_ambiguous_absence` for the next stable release | After that warning ships and its required interval | The additive `require_complete=True` path is strict now and first-party prompt consumers select it. The default remains `False`; the ambiguous legacy path emits the registered warning. Web's direct decoder remains strict without a redundant lookup. An exact changed-default API allowance is required at the eventual cut. |
| C4-01 | Omitted Web request settings change from live environment resolution to construction-bound defaults. | C4 | Behavior | None | After its own stable preview/warning release and interval | Explicit `WebRequestOptions` is additive and first-party factories opt in. No Python default flip or runtime deprecation warning ships in this change. Record an actual stable notice tag and interval before the later default switch; retain dynamic mode through v1 if ineligible. Deferred-open, refresh, and environment-mutation tests preserve both modes. |
| C5A-01 | Web waited interactive mind-map default changes from legacy hydration after failed/removed completion to raising `ArtifactNotReadyError`. | C5a | API default + behavior | None (registry source `since=0.9.0` is unshipped) | v1.0 only after its own stable warning release and interval | Exact key `mind_map_legacy_terminal_hydration`; warn only when Web legacy hydration continues. `failure_policy="raise"` is additive and first-party callers opt in. Android legacy already raises. Require exact changed-default allowance and both-backend behavior matrix at the cut; postpone to a later breaking release if its own interval has not elapsed. |
| C5A-02 | A formerly ignored generation option becomes rejected. No concrete option is approved at this baseline. | C5a | Behavior, possibly API default | None | Per-option; after that option's own stable notice and interval | Create one ledger/registry row per concrete option. This placeholder cannot authorize any rejection. |
| C5B-01 | Remove raw download-prefetch keywords from nine public download methods: `artifacts_data`, `artifacts`, and `mind_maps` as applicable. | C5b | API signatures | None; source registers `artifact_raw_download_prefetch` for the next stable release | After a stable signature warning release and interval, if retirement is retained | One exact API allowance per removed method/keyword break; first-party callers use typed preparation/download. Registered warning: `artifact_raw_download_prefetch` (source planned for 0.9.0; no shipped evidence). Retain until its own interval is met. |

### C9b gate result at this audit

The source implementation prerequisite P4–P7 is present: integrated ownership work commit
`70f0eb44a` is an ancestor of the audited baseline. ADR-0039 now makes the credential destination
reviewable. The release cut is still **not eligible**:

- **Met notice evidence:** P8-08 through P8-10, P8-12 through P8-14, P8-16 through P8-18, and
  P8-29 through P8-34. These shipped in v0.8.1 or earlier and target the next major.
- **Unmet notice evidence:** P8-01 through P8-07, P8-11, P8-15, and P8-19 through P8-28. All rely
  on a stable v0.9.0 that does not exist.
- **Unmet credential migration evidence:** C9-CRED-01 through C9-CRED-05 have no shipped warnings.
- **Unmet later migration evidence:** every C3/C4/C5 row above has no shipped warning; those paths
  must survive v1 if their gates have not matured.
- **Still required at the actual cut:** exact API-break audit and non-stale allowances, drainage of
  the complete v1 registry, deprecation-target validation, migration documentation, clean-process
  Web/Android import isolation, backend/path/profile/env precedence tests, credential mismatch
  before I/O, and the protected release workflow in [releasing.md](releasing.md).

Re-run the remote release/tag/source audit immediately before scheduling C9b. This page records the
2026-09-06 result; it is evidence, not a permanent prediction about future releases.

## Removed in v0.8.0

These error-and-return contract changes completed their v0.7.0 deprecation /
preview cycle and are now the **default** behavior. The full before→after
migration for each is in
[`docs/upgrading-to-0.8.0.md`](upgrading-to-0.8.0.md).

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `sources.get()` / `artifacts.get()` / `notes.get()` / `mind_maps.get()` returning `None` on a miss | `get_or_none()` (warning-free `None`-on-miss), or `try/except SourceNotFoundError` / `ArtifactNotFoundError` / `NoteNotFoundError` / `MindMapNotFoundError` | v0.7.0 | v0.8.0 | A miss now **raises** the matching `*NotFoundError`, unifying the not-found contract with `notebooks.get()`; return annotations narrow from `X \| None` to `X`. The v0.7.0 `DeprecationWarning` (and the `warn_get_returns_none` helper) are gone. [#1247](https://github.com/teng-lin/notebooklm-py/issues/1247) |
| Dict-subscript access (`result["status"]`) on `research.poll` / `research.start` / `research.wait_for_completion`, `artifacts.generate_mind_map`, and `sources.get_guide` returns | Attribute access (`result.status`, `result.sources`, `guide.summary`, …) | v0.7.0 | v0.8.0 | The typed returns (`ResearchTask` / `ResearchStart` / `MindMapResult` / `SourceGuide` / `ResearchSource`) are now pure attribute-only frozen dataclasses: `result["key"]` raises `TypeError`; `result.get(...)` / `.keys()` / `.items()` / `.values()` raise `AttributeError`; `"k" in result` / `iter(result)` / `len(result)` raise `TypeError`. Only attribute access and `to_public_dict()` survive. `ResearchStatus` stays a `str`-enum, so `status == "completed"` keeps working. The `MappingCompatMixin` bridge is removed. [#1251](https://github.com/teng-lin/notebooklm-py/issues/1251) |
| `ResearchAPI.wait_for_completion(interval=...)` | `initial_interval=...` — same cadence, matching `SourcesAPI.wait_until_ready` / `ArtifactsAPI.wait_for_completion` | v0.7.0 | v0.8.0 | The deprecated `interval=` keyword alias is gone; passing it now raises the standard `TypeError` for an unexpected keyword. The `deprecated_kwarg` helper that powered the alias is removed. [#1254](https://github.com/teng-lin/notebooklm-py/issues/1254) |
| `sources.refresh()` / `chat.delete_conversation()` returning `True` | (no replacement — discard the value) | n/a (clean break) | v0.8.0 | Both now return `None`; their annotations change from `-> bool` to `-> None`. The `True` carried no information (any failure raised first). `chat.clear_cache(...)` is unchanged and stays `-> bool`. [#1290](https://github.com/teng-lin/notebooklm-py/issues/1290) |
| Synchronous generation-kickoff refusal swallowed into `GenerationStatus(status="failed")` / returned `None` | Catch the re-raised `RateLimitError` / `RPCError` / `DecodingError` / `ArtifactFeatureUnavailableError` | n/a (clean break) | v0.8.0 | `generate_*` / `revise_slide` / `_parse_generation_result` / `research.start` now **raise** on a "couldn't-start" refusal instead of returning a soft-failed status. `research.start`'s return narrows from `ResearchStart \| None` to `ResearchStart`; `with_rate_limit_retry` retries only on a raised `RateLimitError`. [#1342](https://github.com/teng-lin/notebooklm-py/issues/1342) |
| Derived-read / lister drift collapsing malformed payloads to empty / `None` | Catch the raised `DecodingError` (distinct from a genuine miss) | n/a (clean break) | v0.8.0 | `sources.check_freshness()`, the note lister, and the artifact raw lister now raise `DecodingError` on a structurally-unrecognized payload. Legitimate empty / stale shapes are unchanged. [#1344](https://github.com/teng-lin/notebooklm-py/issues/1344) |
| `notes.update()` / `sources.rename(return_object=False)` / `artifacts.rename(return_object=False)` silently succeeding on a missing target | Catch the raised `*NotFoundError` | n/a (clean break) | v0.8.0 | These now run an existence preflight and raise `NoteNotFoundError` / `SourceNotFoundError` / `ArtifactNotFoundError` on a miss. `return_object=False` still returns `None` on success. [#1362](https://github.com/teng-lin/notebooklm-py/issues/1362) |
| `NotebooksAPI.share()` | `client.sharing.set_public()` + `client.notebooks.get_share_url()` | v0.5.0 | v0.8.0 | The deprecated no-behavior-change wrapper is removed. [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363) |
| `ResearchAPI.poll(task_id=None)` / `wait_for_completion(task_id=None)` silently guessing among multiple in-flight tasks | Pass the explicit `task_id` from `research.start` | v0.6.0 | v0.8.0 | With two or more tasks in flight these now raise the new `AmbiguousResearchTaskError` instead of warning and returning the latest task; with a single in-flight task they resolve it silently. [#1363](https://github.com/teng-lin/notebooklm-py/issues/1363) |
| `NOTEBOOKLM_FUTURE_ERRORS` opt-in preview flag | (no replacement — the previewed behavior is now the default) | v0.7.0 | v0.8.0 | The forward-compat preview gate is removed; setting it is a no-op. The dict-subscript / get-returns-`None` / kwarg-alias deprecation helpers it gated are deleted with it. [#1365](https://github.com/teng-lin/notebooklm-py/issues/1365) |
| `SettingsAPI.get_account_tier()` + the `AccountTier` type (`notebooklm.AccountTier` / `notebooklm.types.AccountTier`) | `client.settings.get_account_limits()` — `AccountLimits.tier` for the subscription tier (since v0.9.0), plus `.notebook_limit` / `.source_limit` for quotas | n/a (clean break) | v0.8.0 | The tier came from `GET_USER_TIER` (live method `FetchRecommendations`, a **promotions** endpoint), a promotion-eligibility signal that could **not** distinguish free from paid — both free and Pro accounts reported `NOTEBOOKLM_TIER_PRO_CONSUMER_USER`. The authoritative quota signal is `AccountLimits`. **Update (v0.9.0):** a *correct* tier signal is now back as `AccountLimits.tier` — an opaque enum read from the authoritative `GET_USER_SETTINGS` limits block (index 4), not the promotions RPC — and the MCP/REST `server_info(include_account=True)` account block exposes a `tier` key again (the removed `plan_name` string does **not** return). |

> **`wait_timeout` was deliberately kept.** The `wait_timeout` keyword on the
> `SourcesAPI.add_*` family (`add_url` / `add_text` / `add_file` / `add_drive`)
> was **not** renamed to `timeout`: on those methods `timeout` would be ambiguous
> with a per-request HTTP timeout, while `wait_timeout` reads as "how long to wait
> for readiness after adding". `SourcesAPI.add_file(mime_type=...)` and
> `notebooklm source add --mime-type` are likewise **not** deprecated —
> `mime_type` sets the resumable-upload content-type header.

> **`notebooklm.rpc` public surface tightened — not a removal (v0.8.0,
> [#1589](https://github.com/teng-lin/notebooklm-py/issues/1589)).**
> `notebooklm.rpc.__all__` now advertises only the two documented power-user
> imports, `RPCMethod` and `resolve_rpc_id`. The ~47 other names it used to list
> — the batchexecute wire helpers (`encode_rpc_request`, `decode_response`,
> `extract_rpc_result`, `safe_index`, …), the endpoint URL constants/helpers, and
> the enum / exception **re-exports** — are **not removed**: they remain
> importable as `notebooklm.rpc.<name>` for back-compat. They were never part of
> the supported public API (`docs/stability.md` has always marked
> `notebooklm.rpc.*` internal); this change only stops the compat gate from
> advertising them. New code should import the canonical public name where one
> exists: most enums as `notebooklm.<X>` / `notebooklm.types.<X>`, but
> `ArtifactStatus` and `artifact_status_to_str` only as `notebooklm.types.<X>`;
> the exceptions as `notebooklm.<X>` / `notebooklm.exceptions.<X>`. The wire
> helpers, the endpoint URL constants/helpers, `safe_index`, `ArtifactTypeCode`,
> and `RPCErrorCode` are internal with **no** blessed public alias and stay
> importable only as `notebooklm.rpc.<name>`. For raw-RPC power use, import
> `from notebooklm.rpc import RPCMethod, resolve_rpc_id`.

> **`notebooklm.auth` public surface tightened — not a removal (v0.8.0,
> [#1592](https://github.com/teng-lin/notebooklm-py/issues/1592)).**
> `auth.__all__` no longer advertises 23 internal re-exports that only first-party
> `src`/tests imported (cookie-snapshot/storage helpers, the WIZ-extraction helpers,
> `authuser_query`/`format_authuser_value`, `load_httpx_cookies`/`normalize_cookie_map`,
> `ALLOWED_COOKIE_DOMAINS`/`MINIMUM_REQUIRED_COOKIES`, the keepalive/refresh env +
> URL constants, `load_auth_from_storage`, `fetch_tokens`, `recover_psidts_in_memory`).
> These were migration leftovers from the `_auth/*` extraction (ADR-0003 → ADR-0014).
> They are **not removed**: each remains importable as `notebooklm.auth.<name>` for
> back-compat — first-party code now imports them from their `notebooklm._auth.<sub>`
> home. `notebooklm.auth.*` has always been internal (`docs/stability.md`) except the
> documented imports (`AuthTokens`, `convert_rookiepy_cookies_to_storage_state`, the
> cookie-domain constants) and the cohesive operations (`enumerate_accounts`,
> `fetch_tokens_with_domains`, `fetch_tokens_passive`, …), which are unchanged. A
> deeper service-interface refactor of the remaining cli/_app-forced names was
> evaluated and deferred (limited encapsulation payoff while names stay importable).


## Removed in v0.7.0

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `NOTEBOOKLM_STRICT_DECODE=0` soft-mode opt-out | Unset the variable (strict is the only mode) | v0.5.0 | v0.7.0 | The env var is now ignored; `safe_index` always raises `UnknownRPCMethodError` on shape drift. Rationale in `docs/stability.md` "Strict decode" + ADR-0011 |
| Positional `wait` / `wait_timeout` on `SourcesAPI.add_url`, `SourcesAPI.add_text`, `SourcesAPI.add_file`, `SourcesAPI.add_drive` | Pass `wait=...` and `wait_timeout=...` as keywords | v0.5.0 | v0.7.0 | `wait` / `wait_timeout` are now keyword-only; positional calls raise `TypeError`. CLI already used keyword arguments |
| `ArtifactsAPI.wait_for_completion(poll_interval=...)` | `initial_interval=...` — same cadence, clearer name | v0.5.0 | v0.7.0 | The `poll_interval` keyword was removed; passing it raises `TypeError` |
| `NotesAPI.create_from_chat(...)` | `ChatAPI.save_answer_as_note(...)` | v0.5.0 | v0.7.0 | Pure deprecated forwarder, now removed (two MINOR cycles of warnings served). `ChatAPI.save_answer_as_note(...)` is the canonical citation-rich saved-from-chat method and data owner (ADR-0013); call it directly. |

## Removed in v0.6.0

| Removed | Replacement | Deprecated since | Removed in | Notes |
|---------|-------------|------------------|------------|-------|
| `NotebookLMClient.rpc_call(source_path=...)` | Omit the argument; the canonical `"/"` default is applied unconditionally | v0.5.0 | v0.6.0 | Public escape-hatch wrapper kept; only the kwarg was cut. No public replacement — callers that need a non-`"/"` source path should add a typed sub-client method (open an issue) rather than reaching across the wrapper. |
| `NotebookLMClient.rpc_call(_is_retry=...)` | Omit the argument | v0.5.0 | v0.6.0 | Internal-only retry flag; never part of the supported public surface. |
| `NotebookLMClient.rpc_call(operation_variant=...)` | Omit the argument | v0.5.0 | v0.6.0 | Internal-only routing key for the mutating-RPC idempotency registry. |

## How deprecations work in this project

* Every deprecated surface emits a `DeprecationWarning` from the call site
  the user wrote, so the warning's `filename`/`lineno` point at user code
  rather than at the library internals.
* Deprecations fire only when the caller reaches the deprecated argument or
  surface. `NotebookLMClient.rpc_call(...)` warns once per client; the generic
  helper otherwise emits on each invocation.
* `NOTEBOOKLM_QUIET_DEPRECATIONS=1` suppresses **every** deprecation warning
  this project emits. The registered auth and raw-call runways are immutable
  `DeprecationSpec` entries routed through `warn_registered_deprecation`; other
  one-off warnings use `warn_deprecated`. All mechanics live in
  `src/notebooklm/_deprecation.py`; ADR-0018 forbids inline
  `warnings.warn(..., DeprecationWarning)` elsewhere and a lint
  (`tests/_guardrails/test_no_inline_deprecation_warnings.py`) enforces it. See
  `docs/configuration.md`.
* `scripts/check_deprecation_targets.py` validates the registry without
  importing it: spec keys and callsites must match, versions must be literal
  semantic versions, removal cannot equal the shipping release, and every
  replacement must resolve structurally on the source tree.
* Not every inline `warnings.warn(...)` is a deprecation. The
  `save_cookies_to_storage(original_snapshot=None)` legacy full-merge path is a
  *permanent* public-API back-compat shim (see
  [`docs/auth-cookie-lifecycle.md`](auth-cookie-lifecycle.md#persistence-concurrency)), not a
  scheduled removal, so it emits
  a **`RuntimeWarning`** safety advisory about the stale-overwrite-fresh race —
  outside ADR-0018's scope and intentionally **not** silenced by
  `NOTEBOOKLM_QUIET_DEPRECATIONS`.
* `NOTEBOOKLM_FUTURE_ERRORS` was the v0.7.0 forward-compat preview gate for the
  v0.8.0 error contract; it was **removed in v0.8.0** now that every break it
  staged is the default, and setting it is a no-op.
* See `docs/stability.md` "Deprecation Policy" for the broader timeline
  contract (one MINOR cycle of warnings before removal during 0.x).

## Removed in past versions

For deprecations that have already completed their removal cycle, see
`docs/stability.md` "Removed in v0.5.0".
