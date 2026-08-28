# ADR-0032: Auth domain types — `Cookie`, `CookieJar`, `MasterToken`, and the boundaries around them

## Status

Accepted. Implementation remains incremental. This extends
[ADR-0031](0031-credential-tier-auth-model.md) (the credential-tier model) from *named operations*
to *named types*. The `Cookie`/`CookieJar` value corrections and pure codec dependency inversion are
implemented first; persistence adoption and the remaining `MasterToken`/`AuthTokens` stages land in
separate reviewed steps. The reasoning that shaped the decision is in Context.

## Context

The `_auth` audit (ADR-0031, #2139) found the subsystem's coupling traces to one gap: nouns have no
methods. A cookie exists in **six** shapes (`DomainCookieMap`, `FlatCookieMap`,
`LegacyDomainCookieMap`, raw Playwright dict, rookiepy dict, "sanitized entries"), `httpx.Cookies` is
a seventh, and the *questions about a cookie set* ("which names?", "is it usable?", "does it still
bind?") are scattered free functions rather than methods — which is what made #2139's boundary moves
unsafe (adapter-local-looking names turned out core-coupled through shared private helpers).

Three findings from design review are load-bearing and non-obvious:

1. **The live jar cannot be an immutable value type.** The running cookie state is an `httpx.Cookies`
   mutated in place by the transport on every response, wholesale-replaced by recovery
   (`_replace_cookie_jar`), and aliased under [ADR-0016](0016-auth-identity-and-core-logger-compatibility.md)'s
   Auth Instance Invariant. Every hard bug this subsystem has fixed — the two-view staleness (fixed
   in the Stage-4 sync commit), the #2057 heal loop, the Appendix-A2 races — is a
   *synchronization-between-views* bug. Making an immutable `CookieJar` the canonical *live*
   representation would add a third view to synchronize and guarantee the next such bug.

2. **`same_site` is contradictory on `Cookie`.** It is load-bearing for persistence (must round-trip
   rookiepy→storage_state or it re-opens the #2150 SameSite downgrade), yet structurally
   unpopulatable from every live source (`http.cookiejar` cannot carry SameSite, so any `Cookie`
   built from an httpx jar has `same_site=None`). Because `Cookie` is a frozen dataclass, `same_site`
   sits in `__eq__`, so any snapshot/baseline diff via `Cookie.__eq__` manufactures phantom SameSite
   deltas and bypasses `_preserved_same_site`.

3. **The *live representation* can't be unified — but `AuthTokens`' shape can still be cleaned.**
   Because the live jar must stay `httpx.Cookies`, there will always be two cookie representations in
   the system: the live wire jar and value-typed `Cookie`/`CookieJar` for inputs/baselines/questions.
   The value of this work is collapsing the six *input* shapes and homing the *questions*, not
   merging those two. Crucially this is NOT a ceiling on `AuthTokens`: the destination (below) removes
   `AuthTokens`' cookie fields entirely, so `AuthTokens` reaches a clean frozen shape precisely by
   *not* holding the live jar at all.

## Decision

Introduce (or fix) these types and boundaries. Value types are immutable and do no I/O; services own
network and disk; the live jar stays `httpx.Cookies`.

**`Cookie`** — frozen, slotted, redacted value. `name, domain, path, value, expires, http_only,
secure, same_site`, with `same_site: str | None = field(default=None, compare=False, repr=False)` —
carried so round-trip preservation works (#2150), never compared so a source that cannot populate it
cannot manufacture deltas. `value` is also excluded from repr, and custom equality keeps assertion
introspection on the redacted representation. Its compared fields `(name, domain, path)` + `(value,
expires, secure, http_only)` are exactly `CookieSnapshotKey + CookieSnapshotValue`. `identity`
remains an exact plain tuple for compatibility while `key` returns the typed `CookieIdentity` tuple.

**`CookieJar`** — genuinely immutable, ordered *sequence* of `Cookie`; construction tuple-copies its
input and frozen slots reject rebinding or deletion. Duplicate resolution is operation-specific:
iteration and `to_storage_rows()` preserve every row, `domain_map_first_wins()` and compatibility
`to_domain_map()` keep the first identity, and direct stdlib insertion in `to_httpx()` uses exact-
identity last-wins behavior while retaining domain/path siblings. The type is for cookie **inputs,
baselines, and questions** — never the live jar, never a `Mapping`.
- construct: `from_storage_state / from_rookiepy / from_domain_map` (exist) + `from_httpx` (new;
  `same_site`-lossy). `from_httpx()` is a valid transient live observation for pure merge decisions,
  but never a durable baseline or a standalone document serializer; persistence adapters that must
  preserve legacy all-domain and HttpOnly observation behavior build their observation explicitly.
- convert: `to_httpx() / to_storage_rows()->list / to_storage_state()->dict /
  domain_map_first_wins()->dict / to_domain_map()->dict`; no `to_flat_map` or ambiguous
  `by_identity` projection. `to_storage_state()` is deliberately the filtered typed view with empty
  origins, not a lossless profile-document round trip.
- ask: `names() / has_secondary_binding() / is_rotatable() / validate_required() / missing_hint()`,
  delegating to the `cookie_policy` tables, which stay a module (they are consumed at extraction
  *failure*, where no jar exists).

**Codec direction** — `cookie_semantics.py` owns dependency-bottom scalar/row mechanics: expiry and
shape normalization, legacy-map and rookiepy adaptation, HttpOnly observation, faithful stdlib
construction, and storage-row serialization. `cookie_types.py` imports only that leaf plus
`cookie_policy`; it never imports compatibility, persistence, recovery, runtime, CLI, or facade
layers. `cookies.py` keeps the legacy free-function identities and logging/policy boundaries as thin
adapters pointing downward to the leaf and values. Thus `cookies → cookie_types → cookie_semantics`
replaces the former upward `cookie_types → cookies` edge.

**`MasterToken`** — pure value: `email, android_id, secret` + trivial accessors. No network, no file
I/O, and no writability logic — `assert_account_writable` reads two disk sources and is only advisory
(the authoritative, TOCTOU-free check is under the write lock), so it belongs to the coordinator, not
the value. `__repr__` is redacted (email + android_id only) like `CookieJar`'s; `secret` never reaches
repr, logs, or errors — its only serialization is `master_token.json` (0600).

**`ProfileStore`** — the persistence boundary: the six `storage_writer` transactions (one lock
template, ADR-0031 Stage 3) plus read/write of `master_token.json` plus the snapshot/delta/CAS
machinery. That machinery keeps its **three explicit equivalence predicates** — tuple-dirty
(excluding `same_site`), value-only CAS, and leading-dot domain-variant matching — as functions, not
`Cookie.__eq__`. `Cookie` unifies the snapshot's *storage*; the comparison policy stays here.

**`MintService`** — `OAuth → MergeSession → RotateCookies → CookieJar`. Network only. The
Tier-0→Tier-1 transition as a service, not a method on the value.

**Bootstrap coordinator** — sequences `MasterToken` + `MintService` + `ProfileStore` + the client
(an upward dependency), and owns writability enforcement. It retains the fan-out that bootstrap
orchestration inherently is; the honest claim is "three testable pieces where there was one," not "a
clean cut."

**`AuthTokens`** — public `@dataclass`, **shape unchanged this release**. Positional construction,
`dataclasses.replace`, and `==` are preserved by not touching the fields. Add `.jar` — a typed
projection of the `cookies` *map* input, explicitly never the live jar.

**Cut** `RequestTokens` and `AccountIdentity` — no behavior, no invariant; keep `csrf_token`/
`session_id` as fields and `resolve_account_identity`'s result as-is.

### The `AuthTokens` destination and its runway

The clean-sheet `AuthTokens` **holds no cookies at all**. The HTTP client already owns the live jar
and mutates it on every response; a second copy on `AuthTokens` is a duplicate fact that needs
eternal syncing, and every hard bug this subsystem has fixed (two-view staleness, #2057, the
Appendix-A2 races) is a symptom of that duplication. So the destination is:

```python
@dataclass(frozen=True)
class AuthTokens:  # a BOOTSTRAP credential, not a live-state bag
    initial_cookies: CookieJar  # immutable seed — read ONCE to open the client, never re-read
    csrf_token: str
    session_id: str
    authuser: int
    account_email: str | None
    storage_path: Path | None
```

This is reachable, and the runway is short, because **the kernel is already the sole internal
live-cookie authority**. The Phase-A audit is equality-pinned by
`tests/_guardrails/test_authtokens_jar_sync.py`; its current inventory is:

| Owner | Shadow access | Role |
|---|---|---|
| `_web/transport/kernel.py:Kernel._bootstrap_cookies` | reads `cookie_jar`, falling back to `cookies` | the one bootstrap hand-off; copied into kernel ownership during client composition |
| `_auth/tokens.py:AuthTokens.__post_init__` | reads/writes `cookies` and `cookie_jar` | public construction compatibility and normalization |
| `_auth/tokens.py:AuthTokens.replace_cookie_jar` | writes `cookies` and `cookie_jar` | public v0.x sync-back only |
| `_auth/tokens.py:AuthTokens._replace_profile_session` | calls `replace_cookie_jar` | syncs public shadows after an atomic stored-profile install |
| `_auth/session.py:_try_storage_cookie_reload` | calls `replace_cookie_jar` in `finally` | syncs public shadows even when cancellation interrupts baseline adoption |
| `_web/transport/auth.py:AuthRefreshCoordinator.update_auth_headers` | calls `replace_cookie_jar` | syncs public shadows after refresh |
| `_auth/tokens.py:AuthTokens.__repr__` | reads both fields | redacted public representation only |
| `_auth/tokens.py:AuthTokens.jar` | reads `cookie_jar` | public question/bootstrap migration projection only |
| `_auth/tokens.py:AuthTokens._flat_cookie_projection` | reads `cookies` | warning-free implementation shared by lossy public compatibility projections |
| `_auth/tokens.py:AuthTokens.flat_cookies` | calls the private projection after warning | directly deprecated public compatibility access only |
| `_auth/tokens.py:AuthTokens.cookie_header` | calls the private projection without warning | distinct domain-blind public compatibility projection only |
| `_auth/tokens.py:AuthTokens.cookie_header_for` | reads `cookie_jar` | public compatibility query; no first-party request path calls it |

The three `replace_cookie_jar` callsites above write only to keep the two public shadows coherent;
none reads a shadow to select recovery or persistence behavior. Persistence reads `kernel.cookies`;
account-email routing now reads the kernel jar before open, while open, and after close. No
first-party post-bootstrap transport, routing, recovery, or persistence decision reads a cookie
shadow. The kernel retains the exact transport jar across close/reopen—it does not construct a
detached generation snapshot.

- **Phase A — now, non-breaking (no public change).** Repoint post-bootstrap readers to
  `kernel.cookies`; `.jar` remains a public projection whose migration role is the future
  `initial_cookies` shape. Label the runtime sync-back compatibility-only. After this,
  `AuthTokens` is *behaviorally* the frozen bootstrap credential above, wearing a mutable-dataclass
  costume for the public surface.
- **Next minor — deprecate, non-breaking.** Runtime `DeprecationWarning` on `flat_cookies` (a plain
  property, safe to warn — the early-warning canary). `cookies` / `cookie_jar` get **docs-only**
  deprecation: they cannot carry a runtime warning because `dataclasses.replace` / `__eq__` /
  `__repr__` read them, so a warning would fire from library internals.
- **Phase B — next major, breaking.** Delete `cookie_jar`, `cookies`, the sync-back, `flat_cookies`,
  and `cookie_header*`; freeze `AuthTokens` to the shape above. Phase A makes this a clean field
  deletion, not a logic migration, because by then nothing internal depends on the fields.

The earlier framing — "`cookies` becomes a property derived from `cookie_jar`" — is rejected as the
destination: it keeps the shadow and computes it, so it does not remove the bug class. The goal is to
**delete the shadow**, not derive it.

## Consequences

- The six input shapes collapse to one constructor family; the questions become methods; the
  free-function fan-out that made #2139 unsafe goes private behind types.
- Pure codec dependencies now point downward. Legacy free-function identities and public exports
  remain intact while the value module has no compatibility or persistence rejoin.
- Projection names make duplicate winners explicit, and `to_httpx()` preserves expiry/session type,
  path, domain spelling, dotted-domain metadata, secure, and HttpOnly without a map collapse.
- `Cookie` retires the parallel `CookieSnapshotKey`/`CookieSnapshotValue` at the storage level while
  the comparison policy stays where the disk state is — a real consolidation, verified not to change
  CAS semantics (the `same_site`/dotted-variant predicates are preserved out-of-band).
- **Honest limits:** the live jar stays `httpx.Cookies` forever (the transport owns it). But that is
  not a ceiling on `AuthTokens` — the destination deletes its cookie fields entirely (see the runway
  above), so `AuthTokens` reaches a clean frozen shape even though the *live jar* is never a value
  type. `from_httpx` is a documented lossy constructor. The bootstrap coordinator's dependency arrows
  are unchanged — it is smaller and testable, not decoupled.
- **Costs:** splitting `master_token.py` and moving the snapshot machinery churn ADR-0007 patch
  seams; the value/service split adds indirection the current flat module lacks; the public-surface
  cleanup (deleting `AuthTokens`' cookie fields) is a real breaking change deferred to a major, though
  Phase A de-risks it to a field deletion rather than a logic migration.

## Alternatives considered

- **Collapse `AuthTokens.cookies`/`cookie_jar` onto an immutable `CookieJar` (the original plan).**
  Rejected: makes the immutable type the live representation, which the transport mutates in place —
  a guaranteed third-view staleness bug. This is the finding that reframed the whole effort.
- **Absorb the snapshot machinery into `CookieJar.changes_since()`.** Rejected: the machinery carries
  deletions, three non-`Cookie.__eq__` equivalence relations, CAS-rejected-key baseline advancement,
  and a filter asymmetry — absorbing it is a rename that would silently change save semantics. It
  stays in `ProfileStore`.
- **Runtime-deprecate `.cookies` this release.** Rejected: the synthesized dataclass methods read it,
  so warnings fire from `replace`/`==`/`repr`. Docs-only until the field can become a property.
- **`MasterToken.mint_session()` / `.is_writable_for()` as methods.** Rejected: minting is network,
  writability needs disk + a lock; both leak I/O onto a pure value. They are a service and a
  coordinator concern respectively.
- **Make `AuthTokens.cookies` a property derived from `cookie_jar` (keep the shadow, compute it).**
  Rejected as the destination: it preserves the two-view duplication that is the bug's root cause. The
  destination deletes the cookie fields; the client is the sole cookie authority.
- **Keep `RequestTokens` / `AccountIdentity`.** Rejected: no behavior or invariant to justify the
  types; they would be renames, not reductions.
