# ADR-0003: `auth.py` write-through facade (`_AuthFacadeModule`)

## Status

**Superseded — closed by [ADR-0014](./0014-feature-local-runtime-adapters.md) (session-decoupling plan Waves 3a + 4 T2.2 + 5).**

The deferred goal — reducing `auth.py` to an almost-flat re-export module — was
completed across three PRs in the session-decoupling plan:

- **#1066** (Wave 3a / Task 2.1) moved `load_auth_from_storage()` body into
  `_auth/tokens.py`. `notebooklm.auth.load_auth_from_storage` became a
  one-line re-export.
- **#1070** (Wave 4 T2.2) **inverted** the `_validate_required_cookies()`
  write-through. Instead of copy-forwarding facade-level rebindings of
  `MINIMUM_REQUIRED_COOKIES` / `_EXTRACTION_HINT` / `_has_valid_secondary_binding`
  into `_cookie_policy` (and mirroring `_SECONDARY_BINDING_WARNED` back),
  `notebooklm.auth._validate_required_cookies` is now identity-equal to
  `notebooklm._auth.cookie_policy._validate_required_cookies`. Tests that
  need to rebind policy names now patch `_auth.cookie_policy.X` directly.
- **#1055** (pre-plan groundwork) had already moved `AuthTokens` into
  `_auth/tokens.py`; the `auth.py`-level binding became a re-export.

**Post-condition (verified by
`grep -nE "^(async[[:space:]]+)?def |^class " src/notebooklm/auth.py`):**
`auth.py` contains exactly one function body, `async def enumerate_accounts`,
which remains to bind `_poke_session` as a default dependency, and zero class
definitions. Every other top-level name is a one-line re-export from the
relevant `_auth/*` module. The historical write-through machinery is fully
retired.

**Why "Superseded by ADR-0014" rather than "Accepted (completed)":** the
original ADR-0003 framing was a *write-through* approach (mirror writes
through the facade). ADR-0014's Rule 3 closes the same goal by *inversion*
(facades become identity-preserving delegates; rebinding happens on the
canonical home). The two approaches are not the same shape, so ADR-0003
is correctly marked superseded rather than promoted.

The rest of this ADR is preserved as the historical record of why the
write-through facade existed at all.

## Context

Authentication concerns (cookie extraction, header construction, refresh, keepalive, account selection, storage on disk) lived in a single `auth.py` module through tier 7. That module reached ~1,600 lines spanning seven loosely-related concerns. Tier 7 (private-module reorg) split it into a `_auth/` subpackage with ten focused modules:

```text
_auth/paths.py            storage paths + filesystem helpers
_auth/extraction.py       cookie/token extraction from browser sessions
_auth/headers.py          HTTP header construction
_auth/cookies.py          cookie maps + _update_cookie_input
_auth/cookie_policy.py    domain allowlist and policy decisions
_auth/account.py          account profile + multi-account switching
_auth/session.py          session-level dataclasses
_auth/storage.py          profile/state persistence on disk
_auth/keepalive.py        cookie keepalive + __Secure-1PSIDTS rotation
_auth/refresh.py          token refresh driver
```

`auth.py` survived the split as a *facade module* that re-exports the public surface (functions, dataclasses, constants) and preserves the `notebooklm.auth.<name>` import path for downstream callers. So far so unremarkable.

What makes this ADR necessary is the *write-through* behavior. The codebase contains ~152 test sites that patch `auth.py`-level names with `monkeypatch.setattr(notebooklm.auth, "<attr>", fake)` (object-attribute form) or `monkeypatch.setattr("notebooklm.auth.<attr>", fake)` (string-target form). Those names had originally lived inside `auth.py`; after the split they live inside `_auth/storage.py`, `_auth/account.py`, `_auth/keepalive.py`, and `_auth/refresh.py`. The patches would silently do nothing if the facade were a passive re-export, because the *consumers* of those names import them directly from the `_auth/*` modules.

**[Superseded]** `_AuthFacadeModule` was retired in D1 PR-2; production no longer mirrors writes. Historically, the mitigation (`src/notebooklm/auth.py:288-339`) was `_AuthFacadeModule`, a subclass of `types.ModuleType` that overrode `__setattr__` to *mirror* writes from `notebooklm.auth` into each owning seam:

```python
class _AuthFacadeModule(ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in _AUTH_STORAGE_FACADE_NAMES:
            setattr(_auth_storage, name, value)
        if name in _AUTH_ACCOUNT_FACADE_NAMES:
            setattr(_auth_account, name, value)
        if name in _AUTH_KEEPALIVE_FACADE_NAMES:
            setattr(_auth_keepalive, name, value)
        if name in _AUTH_REFRESH_FACADE_NAMES:
            setattr(_auth_refresh, name, value)
        # …additional cross-module mirror rules for headers, cookies,
        # cookie_policy, and the _poke_session import alias…
```

The class is installed at module import time with `sys.modules[__name__].__class__ = _AuthFacadeModule`. Four name registries plus two cross-module mirror sets enumerate the names that need write-through; the registries are maintained by hand.

## Decision

`auth.py` is a facade module installed under a `types.ModuleType` subclass whose `__setattr__` mirrors writes into the owning `_auth/*` seam modules. Four name registries (`_AUTH_STORAGE_FACADE_NAMES`, `_AUTH_ACCOUNT_FACADE_NAMES`, `_AUTH_KEEPALIVE_FACADE_NAMES`, `_AUTH_REFRESH_FACADE_NAMES`) and two cross-module mirror sets (`_REFRESH_DEP_MIRROR_NAMES`, `_KEEPALIVE_DEP_MIRROR_NAMES`) enumerate the names that must mirror.

The mechanism is *Accepted* today because:

- It preserves backward compatibility with every existing test that patches `notebooklm.auth.<name>`. Tier 7 would have stalled if the patches had silently no-op'd.
- It is invisible to production callers — read paths are normal `__getattribute__` resolution; only writes (which production never does) take the mirror path.
- The four name registries are small and explicit; new names are added only when a test introduces a fresh patch site.

## Consequences

**Wanted:**

- Tier 7's `auth.py` → `_auth/*` extraction shipped without simultaneously rewriting ~152 test sites. The arc could land incrementally.
- Production behavior is identical to a flat re-export module; the facade has no runtime cost beyond a single `isinstance`-style branch on attribute writes (which production never executes).

**Unwanted (and the reason for the sunset clause):**

- The facade is a *gravity well* for test patterns. Every time a contributor wants to fake an auth helper for a test, the path of least resistance is `monkeypatch.setattr("notebooklm.auth.X", fake)`. That pattern compounds: each new test site adds to the registry that the facade must maintain.
- The four name registries are maintained by hand. When `_auth/storage.py` gains a new function that a test wants to patch, the contributor must remember to add the name to `_AUTH_STORAGE_FACADE_NAMES` *and* re-confirm that no other `_auth/*` module imports the function under its bare name (otherwise the mirror writes only to one of two places).
- The `_REFRESH_DEP_MIRROR_NAMES` / `_KEEPALIVE_DEP_MIRROR_NAMES` cross-module mirror sets encode an even subtler invariant — names that are owned by one seam but aliased into another at import time. A reader has to trace the `from … import …` chains to verify the mirror is complete.
- The whole apparatus exists to make tests pass under a pattern (`monkeypatch.setattr("notebooklm.auth.X", …)`) that an internal architecture audit (disease D1) wants to retire entirely.

The retirement path began in the D1 auth-side PR ([#834](https://github.com/teng-lin/notebooklm-py/pull/834)): the monolithic `tests/unit/test_auth.py` was split into concern-aligned files (`test_auth_storage.py`, `test_auth_account.py`, `test_auth_refresh.py` etc.), monkeypatches were migrated to constructor injection, and `_AuthFacadeModule` itself was deleted. ADR-0014's session-decoupling work finished the second half: `AuthTokens` and `load_auth_from_storage()` now live in `_auth/tokens.py`, `_validate_required_cookies` is a direct `_auth.cookie_policy` re-export, and `async def enumerate_accounts` is the only remaining `auth.py` function body (see the **Status** block above for the current contract).

## Alternatives considered

- **Constructor injection via factories — chosen replacement for the D1 auth-side PR.** Tests construct fakes by calling a `make_fake_core(**overrides)` factory (or the auth-specific equivalent) and inject them through the public constructor instead of patching module globals. The facade becomes unnecessary because no test reaches into `notebooklm.auth.<name>` anymore. Cost: ~70 test sites in `test_auth.py` plus several dozen scattered elsewhere must be rewritten. The migration is sequenced explicitly so the rewrite lands in one auditable PR.
- **Delete `_AuthFacadeModule` outright without migrating tests.** Rejected. The audit measured ~152 object-attribute patches and 58 string-target patches across the test suite, many of them targeting `notebooklm.auth.<name>`. Removing the facade in isolation would break those tests with no actionable diagnostic; contributors would re-add an equivalent mechanism under a different name within a tier or two. (This exact regeneration risk is the reason ADR-0001 / ADR-0002 / ADR-0003 are being written *before* the deletion work — the ADR records the trade-off that prevents the rebuild.)
- **Move the mirror logic into a `__getattr__`-on-module mechanism.** Rejected. `__getattr__` at module level cannot intercept *writes*, only fallback reads. The patches in scope are writes (`monkeypatch.setattr(...)`), so a read-side fallback would not solve the problem.
- **Keep the original monolithic `auth.py` instead of splitting.** Rejected at the time of tier 7. The seven concerns inside `auth.py` had non-overlapping invariants and non-overlapping change cadences; co-locating them was already paying maintenance interest. The split was correct; the facade is the trailing cost of the split done under a test pattern that should not have been load-bearing.
- **Selectively retire the facade names (whittle the registries down).** Rejected. Partial retirement would leave a partial gravity well — easier to grow back than to maintain. The D1 plan is "migrate every site, then delete the whole apparatus in one PR."
