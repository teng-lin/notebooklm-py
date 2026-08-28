# Issue #2172 implementation plan: preserve ADR-0032 and retire the storage seam

**Issue:** <https://github.com/teng-lin/notebooklm-py/issues/2172>
**Planning base:** `origin/main` at `5a49ccd7` (2026-08-10)
**Worktree:** `/home/claude/src/notebooklm-py/.worktrees/issue-2172-auth-followups`
**Branch:** `plan/2172-auth-followups`
**Decision:** preserve accepted ADR-0032; do not implement the proposed `CookieJar` Mapping or the
`AuthTokens.cookies -> CookieJar` field flip

## Goal

Finish two independent auth follow-up arcs without introducing a third live-cookie view, weakening
domain/path routing, or making the deprecated storage facade a permanent runtime dependency:

1. Execute ADR-0032's non-breaking reader audit and v0.9 deprecation runway so `AuthTokens` becomes
   behaviorally a bootstrap credential rather than a live-cookie state bag.
2. Remove `notebooklm._auth.storage.save_cookies_to_storage` as a test patch seam, make the
   `ProfileStore` path unconditional for normal runtime saves, and isolate v0.x result projections
   inside compatibility wrappers.

The eventual deletion of `AuthTokens` cookie fields and freezing of the dataclass belongs to a
linked v1 successor after its remaining mutable responsibilities have an accepted ownership design.

## Selected architecture decision

ADR-0032 remains accepted and is the governing design for this work:

- `CookieJar` remains an immutable, ordered sequence of full-fidelity `Cookie` rows. It does not
  implement `Mapping[str, str]`.
- The kernel-owned `httpx.Cookies` remains the sole live mutable jar. `CookieJar` is used for inputs,
  persistence baselines, and pure questions—not as a second live authority.
- `AuthTokens.cookies`, `cookie_jar`, and related projections remain compatibility shadows during
  the runway. No first-party request or persistence decision may depend on them after bootstrap.
- The rejected A1/A2 route is out of scope: do not add name-only Mapping semantics, do not flip the
  `cookies` field to `CookieJar`, and do not add detached generation-snapshot reconciliation.
- After the runway, a separate accepted v1 design must reconcile ADR-0016, move the remaining mutable
  CSRF/session/account/generation/snapshot responsibilities, delete the cookie shadows and sync-back,
  and only then freeze `AuthTokens`.

This decision preserves a single live authority and removes the synchronization bug class rather
than renaming one of its copies. Update #2172 to record that its literal Stage 4 was rejected in
favor of ADR-0032 and link the v1 successor before closing the issue.

## Current-state findings

| Finding | Current evidence | Planning consequence |
|---|---|---|
| ADR-0032 rejects the issue's literal Arc 1 destination | The accepted ADR defines `CookieJar` as an ordered sequence, “never a Mapping,” and rejects collapsing `cookies`/`cookie_jar` | Implement A0 only; the proposed A1/A2 field/container changes are not work items |
| `CookieJar` already has row-preserving sequence semantics | Production code tuple-copies and iterates it in `_web/transport/cookie_persistence.py`, `tokens.py`, `recovery.py`, `cookie_merge.py`, and `profile_store.py`; tests pin `next(iter(jar))` and duplicate rows | Preserve iteration and `len()` semantics; do not introduce key iteration or unique-name counts |
| Flat projection is lossy and operation-specific | `flatten_cookie_map()` uses domain priority and first-wins within a tied tier, while the issue proposed last-wins across domains | Keep flat projections compatibility-only and prohibit their use in transport or persistence paths |
| ADR-0032's reader evidence is stale | `_auth/account_email.py` reads `auth.cookie_jar` as a closed-client fallback, and ADR-0016 preserves one mutable `AuthTokens` identity | A0.1 must refresh the audit and resolve the fallback; the v1 successor must own the broader identity design |
| Arc 2's normal typed path is already behaviorally present | `ClientLifecycle.save_cookies()` selects `CookiePersistence._save_canonical()` when the default storage symbol is untouched and uses the adapter only for a patched or injected saver | Treat the typed path as the baseline; remove only the transitional module-identity fallback |
| The live patch population is four sites | Two patches are in `test_runtime_lifecycle.py`, one in `test_auth_issue_2061.py`, and one nested patch is in `test_cookie_persistence_profile_store.py` | Migrate four sites in three files and add a guard that sees the nested target missed by the broad audit |

## Scope and PR sequence

The two arcs may develop in parallel. Within each arc, preserve the order below:

```text
Arc 1: A0.1 reader/ownership audit → A0.2 v0.9 deprecation runway → linked v1 successor

Arc 2: B1 patch-site migration → B2 unconditional normal path → B3 native first-party results
                                                           └── B4 optional store threading
```

Required implementation PRs are A0.1, A0.2, B1, B2, and B3. B4 is non-blocking. Each PR must be
independently green and reversible. The linked v1 successor is required issue-disposition work, but
its implementation is not part of #2172.

## A0.1 — finish ADR-0032 Phase A's non-breaking reader audit

**Purpose:** make “bootstrap credential, not live-state bag” true before deprecating the public
cookie shadows.

### Changes

- Re-run a repository-wide read/write audit for `AuthTokens.cookies`, `.cookie_jar`, `.jar`,
  `.flat_cookies`, `.cookie_header`, and `.cookie_header_for`; record every production result and its
  bootstrap/post-open role in ADR-0032.
- Repoint post-open first-party cookie reads to the kernel-owned `httpx.Cookies`. Explicitly resolve
  `_auth/account_email.py`'s closed-client fallback and test it; do not delete the public shadow while
  that fallback still depends on its final live generation.
- Label public-shadow assignments in `_web/transport/auth.py` compatibility-only. Prove kernel and
  persistence behavior never reads them to make routing or save decisions.
- Reconcile ADR-0032's stale “no post-open reader” statement with ADR-0016's Auth Instance Invariant.
  Document which mutable responsibilities remain for the v1 successor.
- Preserve the current public constructor, dataclass fields, positional behavior, equality, repr,
  `dataclasses.replace()`, warning behavior, and `CookieJar` sequence protocol.

### Expected files

- `src/notebooklm/_auth/account_email.py`
- `src/notebooklm/_auth/tokens.py`
- `src/notebooklm/_web/transport/kernel.py`
- `src/notebooklm/_web/transport/auth.py`
- `tests/unit/test_client_account_email.py`
- `tests/unit/test_runtime_auth.py`
- `tests/_guardrails/test_authtokens_jar_sync.py`
- `docs/adr/0016-auth-identity-and-core-logger-compatibility.md`
- `docs/adr/0032-auth-domain-types.md`

### Acceptance criteria

- A structural audit has an equality-pinned, stale-detecting inventory of all cookie-shadow reads
  and writes.
- Account-email behavior works both inside and outside client context without making a request or
  persistence path depend on an `AuthTokens` shadow.
- Kernel seeding may read the bootstrap projection once; no post-open transport, routing, recovery,
  or persistence decision reads a cookie shadow.
- Public signatures, dataclass fields, equality, repr, `dataclasses.replace()`, and warning behavior
  are unchanged.
- `CookieJar.__iter__` still yields `Cookie` rows, `len(jar)` still counts rows, and duplicate
  domain/path siblings remain intact.

## A0.2 — ship the v0.9 deprecation runway

**Purpose:** give callers an enforceable migration path without changing field behavior.

### Changes

- Register one runtime deprecation for direct `flat_cookies` reads with literal `since="0.9.0"`,
  `removal="1.0"`, public replacement guidance, and caller-attributed stacklevel.
- Keep `cookies` and `cookie_jar` docs-only deprecated because synthesized dataclass operations read
  fields and must not emit warnings from `repr`, equality, or `dataclasses.replace()`.
- Mark `jar`, `cookie_header`, and `cookie_header_for` explicitly in the deprecation table:
  - `jar` is the migration shape for the future `initial_cookies` bootstrap field;
  - `cookie_header` and `cookie_header_for` are scheduled for v1 deletion;
  - `cookie_header_for` users are directed to managed-client request APIs.
- Extract one private warning-free flat projection. Both compatibility properties may use it, but
  only direct public `flat_cookies` access calls `warn_registered_deprecation`; `cookie_header` must
  not acquire an indirect warning or an internal stack attribution.
- Update the registered-key inventories and deprecation tables atomically.

### Expected files

- `src/notebooklm/_deprecation.py`
- `src/notebooklm/_auth/tokens.py`
- `scripts/check_deprecation_targets.py`
- `tests/unit/test_deprecation_helper.py`
- `tests/unit/test_check_deprecation_targets.py`
- `tests/_guardrails/test_auth_cookie_docs.py`
- `docs/deprecations.md`
- `docs/python-api.md`
- `docs/auth-cookie-lifecycle.md`
- `CHANGELOG.md`

### Acceptance criteria

- Direct `flat_cookies` access emits exactly one warning with the public caller filename and line.
- `NOTEBOOKLM_QUIET_DEPRECATIONS` suppresses that warning.
- `cookie_header`, construction, repr, equality, and `dataclasses.replace()` remain warning-free.
- The default deprecation-target checker and its exact registry-key inventories pass.
- Docs describe `CookieJar` as an ordered sequence and never suggest it is a Mapping or live jar.
- #2172 records the selected ADR-0032 route and links the v1 successor.

## Linked v1 successor boundary

The successor is a required deliverable, but its implementation is explicitly outside this plan.
It owns:

- an accepted ownership design for mutable CSRF/session tokens, account route, profile-session
  generation, and persistence snapshots;
- reconciliation of that design with ADR-0016's single mutable `AuthTokens` identity;
- removal of `cookies`, `cookie_jar`, `flat_cookies`, `cookie_header`, `cookie_header_for`, and the
  compatibility sync-back after their runway;
- introduction of the final immutable `initial_cookies: CookieJar` bootstrap field;
- freezing `AuthTokens` only after every mutable responsibility and post-open reader is moved;
- removal of every other deprecation whose target has lapsed by the shipping v1 release.

The successor must not make immutable `CookieJar` the live transport representation and must not add
a detached generation snapshot to `AuthTokens`.

## B1 — migrate module patch sites and add the gate

**Purpose:** remove tests' dependency on rebinding
`notebooklm._auth.storage.save_cookies_to_storage` before deleting the fallback.

### Migrations

- `tests/unit/test_runtime_lifecycle.py`
  - Delete the two late-binding-only assertions at current lines ~538 and ~814.
  - Retain and retarget the existing typed-default coverage (~568-586) and explicit saver wiring
    coverage (~885-901); do not add duplicate injection tests.
- `tests/unit/test_cookie_persistence_profile_store.py`
  - Delete the nested `persistence_module._auth_storage` branch at current line ~484.
  - Retain the combined test's explicit custom-saver branch (~494-512) and rename the test around
    default canonical versus explicit override behavior.
- `tests/unit/test_auth_issue_2061.py`
  - Remove the negative global patch. Prove “in-memory recovery does not save” through observable
    state or a narrow injected collaborator at the recovery boundary; do not patch `ProfileStore` at
    class level.
- Keep `tests/unit/test_web/transport/cookie_persistence.py`'s defensive-copy, `asyncio.to_thread`, and ordering
  coverage as the behavioral oracle for explicit savers.
- Reuse `tests/_helpers/client_factory.py` for client-level injection. Add a small recording fake in
  `tests/_fixtures/` only if repeated call/result behavior justifies it; do not hide the same module
  patch inside a fixture.

### Guardrail

Add `tests/_guardrails/test_no_storage_cookie_saver_patches.py`. It scans every Python file under
`tests/`, including `conftest.py`, `_helpers/`, and `_fixtures/`, and rejects mutation of an attribute
named `save_cookies_to_storage` through:

- positional and keyword `monkeypatch.setattr`;
- direct same-scope aliases (`mp = monkeypatch`, `setter = monkeypatch.setattr`) and import aliases;
- `patch`, `patch.object`, and `mock.patch.object`;
- nested attribute targets such as `persistence_module._auth_storage`;
- literal string targets ending in `.save_cookies_to_storage`;
- direct `setattr`, attribute assignment/deletion, `monkeypatch.delattr`, and
  `monkeypatch.setitem(module.__dict__, ...)`;
- positional, keyword (`target=`, `name=`, `attribute=`), and mixed call spellings, including
  same-module constant strings that can be resolved statically.

The guard analyzes mutation contexts, not arbitrary mentions: direct compatibility-wrapper calls,
imports, assertions, and standalone fixture strings remain allowed. Its alias model is bounded to
same-scope static assignments/imports; aliases propagated through calls, containers, or dynamic
`getattr` are outside the static proof and called out in the guard docstring. Self-test every
prohibited and allowed spelling with parsed source, then run a live repository scan.

Regenerate `tests/fixtures/baselines/auth_patch_sites.json` and the exact projections in
`tests/unit/test_audit_auth_patch_sites.py` after removing the three sites the existing collector can
see. Keep the new dedicated guard as the authority for the nested fourth site; do not silently teach
the broad audit new resolution semantics unless its full baseline is intentionally reviewed.

### Acceptance criteria

- The dedicated guard reports zero sites.
- Its live scan and an independent `rg` inspection find no mutation targeting
  `save_cookies_to_storage`; retained direct wrapper tests remain legal.
- `auth_patch_sites.json` and `test_audit_auth_patch_sites.py` match the post-migration broad-audit
  inventory with no stale count.
- Explicit injected savers still run in `asyncio.to_thread`, preserve call ordering, receive copied
  jars, and surface failures/logging exactly as before.
- Default saves still go through `ProfileStore.merge_cookie_observation`.

## B2 — remove the module fallback; make the normal path unconditional

**Purpose:** ensure production runtime behavior never depends on the identity of a mutable module
attribute.

### Changes

- Delete `_CANONICAL_COOKIE_SAVER` and `_canonical_cookie_saver_is_current()` from
  `_web/transport/cookie_persistence.py`.
- Delete `_default_cookie_saver` and the `_uses_default_cookie_saver` state used only to recognize a
  patched default.
- In `ClientLifecycle.save_cookies()`:
  - when `cookie_saver is None`, always call the canonical typed
    `CookiePersistence`/`ProfileStore` path;
  - when an explicit `cookie_saver` is supplied, route only through
    `CookiePersistence._save_v0_callback` (final name may vary, role may not). This adapter owns
    defensive copying, worker-thread invocation, and `bool | CookieSaveResult` baseline projection.
    No normal store-backed path imports or branches on `CookieSaveResult`.
- Update lifecycle, client construction, runtime assembly, and ADR comments so they no longer
  promise that patching `_auth.storage.save_cookies_to_storage` changes a live client.
- Update structural/re-export touchpoints in `src/notebooklm/_runtime/__init__.py`, lifecycle
  imports/`__all__`, `tests/_guardrails/test_auth_storage_compatibility.py`,
  `tests/_guardrails/test_cookie_persistence_boundary.py`, and
  `tests/unit/test_cookie_persistence_profile_store.py`. `_runtime` must not re-export the deleted
  helper.
- Replace structural tests that require two default routes with gates that require one normal route,
  one named compatibility adapter, and an equality-pinned allowlist for legacy-result imports.
- Keep `storage.save_cookies_to_storage` as a plain v0.x facade wrapper with its signature, warning,
  return, and direct-call tests intact. It must no longer be imported or inspected to decide normal
  runtime behavior.
- Emit a value-free debug event selecting `canonical_store` versus `explicit_v0_callback`; include
  path/status/type only and never cookie values.

### Compatibility caveat

`NotebookLMClient(cookie_saver=...)` is documented and frozen in ADR-0034. Deleting every alternate
callback path would be a separate public break. Retain an explicitly injected compatibility adapter.
If maintainers later remove `cookie_saver`, give it a registered deprecation and separate v1 removal
rather than hiding that break inside seam cleanup.

### Acceptance criteria

- Grep finds no `_CANONICAL_COOKIE_SAVER`, `_canonical_cookie_saver_is_current`,
  `_default_cookie_saver`, or module-identity branch.
- Rebinding `_auth.storage.save_cookies_to_storage` after client construction has no effect and is
  prohibited in tests.
- Close, keepalive, and refresh use the same typed store path by default.
- Explicit `cookie_saver=` tests continue to cover the documented override.
- The callback adapter is the only `_web/transport/cookie_persistence.py` function allowed to consume
  `CookieSaveResult`; an AST/import guard pins that exception and fails stale entries.
- Cancellation, later-sequence-wins, accepted/CAS-rejected baseline advancement, and close waiting
  for keepalive teardown remain covered, including
  `tests/unit/concurrency/test_session_close_refresh_race.py` and
  `tests/unit/test_auth_keepalive.py`.
- `storage.save_cookies_to_storage` remains directly importable from its compatibility homes.

## B3 — make native store results load-bearing in first-party flows

**Purpose:** stop first-party code translating between parallel enums while retaining v0.x wrapper
returns for compatibility.

### Native result ergonomics

- Add value-free convenience properties to `ReplaceResult` (`ok`, `lock_unavailable`,
  `required_cookies_dropped`) so callers do not need facade enums.
- Keep `CookieMergeResult` as the only first-party cookie-merge result.
- Add invariant tests for every `ReplaceStatus` and `CookieMergeDisposition` member.

### First-party caller migration

- Browser capture/re-mint constructs `RemintWriteRequest` and consumes
  `ProfileStore.replace_from_remint() -> ReplaceResult` directly inside `_auth`, rather than calling
  `storage.replace_from_remint() -> WriteOutcome`.
- `_app/login_cookie.py`, `cli/_cookie_import.py`, `cli/services/login/cookie_writes.py`, and
  `cli/services/login/refresh.py` consume `ReplaceResult` rather than `LoginWriteOutcome`.
  The cookie-import adapter injects `replace_profile_from_login` with `account_mode="keep"`, not the
  compatibility `replace_from_login` wrapper.
- Add this exact path-shaped native operation in `profile_migration.py` (or an equally narrow
  non-compatibility owner):

  ```python
  def replace_profile_from_login(
      path: Path,
      state: Mapping[str, Any],
      *,
      include_domains: set[str] | None,
      include_optional: bool = False,
      account_mode: Literal["keep", "clear", "set"] = "keep",
      account_authuser: int | None = None,
      account_email: str | None = None,
      backup: bool = False,
  ) -> ReplaceResult: ...
  ```

  `keep`/`clear` require both account values to be `None`; `set` requires a non-`None` authuser and
  accepts the optional email. Invalid mode/value combinations fail before I/O, without adding
  stricter value validation to the public compatibility wrapper. The operation maps primitives to
  dependency-lower `KeepAccount`/`ClearAccount`/`SetAccount(ProfileAccount(...))`, then constructs
  `ProfileStore`, `LoginWriteRequest`, and `LoginProfileWriter` once. Compatibility-owned
  `AccountRecord`, `KEEP_ACCOUNT`, and `CLEAR_ACCOUNT` never flow downward into this module.
- Alias the operation and `ReplaceResult` through `notebooklm.auth`, ledger both in
  `AUTH_CROSS_BOUNDARY_NAMES`, and keep them out of public `__all__`. Update `_app`'s `LoginWriter`
  protocol and the two CLI dependency objects to the exact primitive signature. Import/capture uses
  `keep`, authenticated cookie login uses `set`, and default-account refresh uses `clear`.
- Keep `storage.replace_from_login(...) -> LoginWriteOutcome` unchanged as a v0.x wrapper that calls
  the new native operation, translates its `AccountRecord`/sentinels to primitive arguments, and
  projects its result. Keep the compatibility-only `io_policy` parameter at this wrapper. Test every
  translation and invalid native combination. Do not make CLI code import `_auth`, and do not
  publish `ProfileStore`, `LoginWriteRequest`, account directives, or `ReplaceResult` as supported
  external API.
- `CookiePersistence` and PSIDTS recovery consume `CookieMergeResult` on the normal path.

### Compatibility wrapper isolation

- Keep `WriteOutcome`, `LoginWriteOutcome`, and `CookieSaveResult` only for old wrapper signatures
  in `storage.py`/`storage_writer.py`, facade aliases scheduled for next-major deletion, and the
  single B2 callback adapter that interprets the documented custom-saver return.
- Centralize native-status projection inside those wrappers. Use module-constant maps whose key sets
  equal exactly `set(ReplaceStatus)`/`set(CookieMergeDisposition)`; map statuses impossible for a
  wrapper to a named contract-violation function rather than an open-ended “unreachable” chain.
- Assert the maps' exact key sets. Adding an enum member must fail until its projection is added; do
  not synthesize or monkeypatch Enum members at runtime.

### Acceptance criteria

- Searches outside compatibility owners, facade aliases, direct wrapper tests, and the single named
  callback adapter find no first-party dependency on `WriteOutcome`, `LoginWriteOutcome`,
  `WriteStatus`, `LoginWriteStatus`, or `CookieSaveResult`. The exact exception set is
  equality-pinned and stale entries fail.
- First-party behavior tests assert native results directly.
- `_app` and CLI import the typed operation/result only through the first-party facade ledger; the
  no-private-import and no-unused-ledger guards pass.
- Direct compatibility-wrapper tests prove exact legacy return types, statuses, warnings, errors,
  and facade identity.
- Projection maps cover exactly the native enum members and every projection is behavior-tested.
- A checked caller-to-test inventory maps every production `replace_from_remint` and
  `replace_profile_from_login` callsite, including the CLI cookie-import adapter, to a named behavior
  test and fails for uncovered callers and stale entries.

## B4 — optional store-threading cleanup

B4 is useful but must not block B1–B3.

- In `psidts_recovery.py`, resolve one `ProfileStore` at `_recover_psidts_inline` and thread it
  through read, rotate-save, and persisted-state checks.
- In `refresh.py`, add an internal store-accepting operation and let path-based compatibility
  `fetch_tokens_with_domains()` construct once at its outer boundary. Preserve the
  `FileAuthSource.store` already carried by `StoredAuthLoader`.
- Keep `merge_cookie_delta(path, ...)` as a path-shaped v0.x wrapper, but move work to a private
  store-accepting helper so construction occurs once at the compatibility boundary.
- Prefer threading an existing store through `CookiePersistence` override paths. A
  `store_factory(path)` is acceptable only where the caller genuinely owns only a path.
- Extend `tests/_guardrails/test_auth_profile_store_boundary.py` with an equality-pinned,
  stale-detecting allowlist for `ProfileStore(...)` construction at approved boundaries. Reuse its
  existing alias/escape analysis. Do not make `ProfileStore` public or patch its methods globally.

## Verification matrix

Run each focused block in its owning PR. Run the complete repository gate after the last required
PR.

### A0.1/A0.2 — ADR-0032 route

```bash
uv run pytest tests/unit/test_client_account_email.py \
  tests/unit/test_runtime_auth.py \
  tests/unit/test_auth_storage.py \
  tests/unit/test_auth_stored_auth.py \
  tests/unit/test_deprecation_helper.py \
  tests/unit/test_check_deprecation_targets.py \
  tests/_guardrails/test_authtokens_jar_sync.py \
  tests/_guardrails/test_auth_cookie_docs.py \
  tests/_guardrails/test_public_surface.py
uv run python scripts/check_deprecation_targets.py
uv run python scripts/audit_public_api_compat.py --check-stale
```

The API audit must show no unapproved structural break: A0 preserves the public dataclass and
`CookieJar` protocol. The behavioral tests pin the warning-free synthesized dataclass operations and
the sequence-shaped `.jar` projection.

### B1 — patch-site migration and guard

```bash
uv run pytest tests/unit/test_runtime_lifecycle.py \
  tests/unit/test_web/transport/cookie_persistence.py \
  tests/unit/test_cookie_persistence_profile_store.py \
  tests/unit/test_auth_issue_2061.py \
  tests/unit/test_audit_auth_patch_sites.py \
  tests/_guardrails/test_no_storage_cookie_saver_patches.py
uv run python scripts/audit_auth_patch_sites.py --module storage --json
uv run python scripts/audit_auth_patch_sites.py --module storage --list-sites
```

### B2 — unconditional default path

```bash
uv run pytest tests/unit/test_runtime_lifecycle.py \
  tests/unit/test_web/transport/cookie_persistence.py \
  tests/unit/test_cookie_persistence_profile_store.py \
  tests/unit/test_auth_cookie_save_race.py \
  tests/unit/test_cookie_save_ordering.py \
  tests/unit/test_auth_keepalive.py \
  tests/unit/concurrency/test_session_close_refresh_race.py \
  tests/_guardrails/test_cookie_persistence_boundary.py \
  tests/_guardrails/test_auth_storage_compatibility.py
```

### B3 — native results and first-party callers

```bash
uv run pytest tests/unit/test_storage_writer.py \
  tests/unit/test_auth_profile_migration.py \
  tests/unit/test_auth_profile_store.py \
  tests/unit/test_auth_profile_store_login.py \
  tests/unit/test_auth_profile_store_remint.py \
  tests/unit/test_browser_capture_headless_arm.py \
  tests/unit/test_browser_capture_cdp_arm.py \
  tests/unit/app/test_app_login_cookie.py \
  tests/unit/cli/test_login_cookie_app_adapters.py \
  tests/unit/cli/test_cookie_writes.py \
  tests/unit/cli/test_login_refresh_coverage.py \
  tests/unit/cli/test_playwright_login_render_contract.py \
  tests/_guardrails/test_auth_profile_store_boundary.py \
  tests/_guardrails/test_storage_writer_boundary.py \
  tests/_guardrails/test_auth_storage_compatibility.py \
  tests/_guardrails/test_public_surface.py
```

`tests/unit/test_auth_profile_migration.py` directly owns native-operation mode validation,
account-directive translation, and one-time writer/store construction. The checked caller-to-test
inventory must map every native login/remint caller to a suite executed by this block.

### B4 — optional store threading

```bash
uv run pytest tests/_guardrails/test_auth_profile_store_boundary.py \
  tests/unit/test_auth_profile_store.py \
  tests/unit/test_auth_issue_2061.py \
  tests/unit/test_auth_psidts_recovery.py \
  tests/unit/test_auth_refresh.py \
  tests/unit/test_auth_refresh_profile_store.py \
  tests/unit/test_auth_stored_auth.py \
  tests/unit/test_cookie_persistence_profile_store.py
```

### Final repository gate

Run this in CI or provision the same optional adapter dependencies so MCP, server, and impersonation
coverage cannot disappear behind import skips:

```bash
uv sync --frozen --extra browser --extra dev --extra markdown \
  --extra mcp --extra server --extra impersonate
uv run ruff check .
uv run ruff format --check .
uv run mypy src/notebooklm
uv run pytest -n auto --dist=loadgroup \
  --cov=src/notebooklm \
  --cov-report=term-missing \
  --cov-report=json:coverage.json \
  --cov-fail-under=90
uv run python scripts/check_coverage_thresholds.py --coverage-json coverage.json
uv run python scripts/check_deprecation_targets.py
uv run python scripts/audit_public_api_compat.py --check-stale
uv run pre-commit run --all-files
git diff --check
```

## Risk controls

- **Single live authority:** the kernel's `httpx.Cookies` is the only live jar. Do not add a detached
  generation snapshot or make a public `AuthTokens` shadow load-bearing after bootstrap.
- **Credential secrecy:** `CookieJar` and the compatibility projections contain credentials. Repr,
  logs, warnings, exceptions, library assertion diagnostics, and result objects never include cookie
  values.
- **Routing correctness:** no first-party transport, routing, or persistence path consumes a
  flat-name view. The deprecated `cookie_header` may retain its domain-blind public behavior during
  the runway, but a source guard proves no library request path calls it.
- **SameSite correctness:** never use `CookieJar.from_httpx()` as a durable persistence baseline.
- **Deprecation correctness:** direct `flat_cookies` access warns once at the caller; synthesized
  dataclass operations and `cookie_header` stay quiet. Docs-only field deprecations must not be
  represented as runtime warnings.
- **Concurrent saves:** preserve per-path ordering, CAS-rejected baseline advancement, defensive jar
  copying, and close-after-keepalive ordering.
- **Public compatibility:** A0 introduces no structural public break. `CookieJar` sequence behavior,
  `AuthTokens` construction/equality, and the documented `cookie_saver=` override remain pinned.
- **No hidden seam replacement:** injected instance fakes are allowed; process-global class patches
  of `ProfileStore` are not.
- **Bounded compatibility island:** only v0.x facade wrappers and the single explicit callback
  adapter may consume legacy result types; equality-pinned guards prevent spread or stale entries.
- **PR independence:** Arc 1 and Arc 2 may develop in parallel, but each arc's internal order is
  strict.

## Rollout, observability, and rollback

- A0.1 and A0.2 are independently revertible. Reverting A0.2 removes the warning/docs runway without
  changing stored formats or live-cookie ownership.
- B1 and B2 are independently revertible before release. B3 is revertible while the legacy wrappers
  remain; reverting it restores first-party callers, not storage formats.
- Value-free debug logs distinguish `canonical_store` from `explicit_v0_callback` saves and record
  only path, disposition/status, and exception type.
- Do not deprecate `NotebookLMClient(cookie_saver=...)` opportunistically. A future removal requires
  its own registered runway and compatibility issue.
- After A0.2 and B1–B3 land, update #2172 with the ADR-0032 decision, close it, and link the v1
  ownership/freeze successor. Do not leave #2172 open across the major-version boundary.
- If a patch release exposes a regression, restore the prior reader or store call path without
  changing cookie formats or introducing a new live snapshot.

## Definition of done

Issue #2172 is complete only when:

- ADR-0032 and ADR-0016 contain a current, equality-pinned reader/ownership audit;
- no first-party post-open transport, routing, recovery, or persistence decision reads an
  `AuthTokens` cookie shadow;
- the v0.9 deprecation runway is shipped with correct direct-access warning behavior and quiet
  synthesized dataclass operations;
- #2172 records preservation of ADR-0032, rejects the literal Mapping/field-flip route, and links a
  scoped v1 ownership/freeze successor;
- no test patches `save_cookies_to_storage`, including alias, keyword, nested-attribute, and string
  mutation spellings covered by the documented static model;
- normal close, refresh, and keepalive saves unconditionally use the store-backed path and no module
  identity sentinel remains;
- normal first-party flows use `ReplaceResult` and `CookieMergeResult`; old result shapes are
  confined to v0.x wrappers and the explicit `cookie_saver=` compatibility adapter;
- all focused, compatibility, type, formatting, full-suite, and pre-commit gates pass;
- no change turns `CookieJar` into a Mapping, flips `AuthTokens.cookies` to `CookieJar`, adds a
  detached generation snapshot, publishes `ProfileStore`, makes it a global patch target, or pulls
  ADR-0031 Stage 5 into scope.
