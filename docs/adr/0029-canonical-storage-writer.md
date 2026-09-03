# ADR-0029: Single canonical `storage_state.json` writer

## Status

Accepted (rolling out — refactor (b): b-PR1 lands the writer + guardrail; b-PR2
migrates browser capture / L4 re-mint; b-PR3 migrates the three CLI login/import
writers onto `replace_from_login`, lands the runtime `atomic_write_json`
storage-state rejection + module-private bypass, and shrinks the storage-state
exemption to `{migration.py}`).

**Amended by [ADR-0033](0033-auth-consolidation-policy.md) (persistence merge):**
the single sanctioned home is now `_auth/storage.py`, which absorbed this
module; the private `storage_writer.py` compatibility shim was subsequently
removed early by ADR-0033's explicit 2026-09-02 policy override. The boundary this
ADR establishes is unchanged in substance but is now enforced at **function**
granularity — an equality-asserted allowlist of the intent-writer function names
permitted to reach the `_atomic_io` bypass — because a module-granular assertion
over the merged persistence module would no longer constrain much. The Decision
below is left as written: it records what was decided then.

Scope of b-PR1 (per plan §b.6): **relocations + additive enforcement + the
[storage-F3] save-ordering guard**. The relocations are behaviour-preserving for
the happy path; the additive parts that DO change observable behaviour are, by
design: (a) the account/master-token writers' lock-failure exception type
(`filelock.Timeout` → `LockUnavailableError`, still an `OSError`) and worst-case
wait (10 s → 90 s), and (b) the save-ordering guard in `_cookie_persistence.py`,
which makes a queued stale save drop itself instead of overwriting a newer one.
"Behaviour-preserving" below refers specifically to the write mechanics of the
relocated writers, not to these two intended additions.

## Context

The cookie/auth audit found that the invariants protecting `storage_state.json`
lived in comments, not enforcement, and were violated in practice:

- Writes happened from many call sites — `_auth/storage.py` (cookie CAS merge),
  `_auth/account.py` (in-band account metadata), `_auth/master_token.py`
  (L4 re-mint persist + `master_token.json`), `_browser/browser_capture.py`, and
  several `cli/services/login/` writers — each with its own locking (or none).
- The storage-sentinel lock was spelled two ways: cookie saves used the dotted
  `.storage_state.json.lock` sibling via the project-internal `_file_lock`
  (`fcntl.flock` / `msvcrt`), while account/master-token writers used
  `filelock.FileLock`. They interoperated only by the accident of both using
  `fcntl.flock` on POSIX.
- `master_token.py`'s `persist_minted_jar` / `write_master_token` hand-rolled
  their writes with no `fsync`, no temp cleanup, and (for `write_master_token`)
  no lock at all ([storage-F5]).
- Queued cookie saves could reorder so a stale save wrote last, silently
  overwriting fresh cookies ([storage-F3]).

The one *enforced* invariant — `atomic_update_json` rejecting
`storage_state.json` paths (#1215) — held, which is the pattern this ADR
generalises.

## Decision

Introduce `src/notebooklm/_auth/storage_writer.py` as the **single sanctioned
home** for mutating `storage_state.json`. It is the only module under `_auth`
allowed to import the `_atomic_io` write primitives (`atomic_write_json` /
`replace_file_atomically`) and to perform the final atomic write. It exposes an
intent-shaped, all-synchronous API: `merge_cookie_delta` (CAS delta merge),
`update_account_metadata` / `clear_in_band_account` (in-band account), and
`persist_minted_jar` / `write_master_token` (master-token).

### Compatibility-facade continuity

`_auth/storage.py` keeps `save_cookies_to_storage` as the directly importable
v0.x facade that forwards to the native cookie merge. Hosts that need the
callback contract pass it explicitly through `NotebookLMClient(cookie_saver=...)`;
the normal lifecycle route is an unconditional typed `ProfileStore` merge and
does not late-bind or inspect the facade symbol. The CAS math helpers
(`_merge_cookies_with_snapshot`, snapshot/baseline helpers, `CookieSaveResult`)

### One lock, unified and bounded

The full-file RMW / re-mint intents drop `filelock` in favour of the
project-internal `_file_lock` primitive (`filelock` stays for `migration.py` and
`context.json`). The acquire is **platform-neutral bounded**: a non-blocking
probe plus deadline/jitter retry (default **90 s**, up from `filelock`'s 10 s).
An in-process `threading.Lock` keyed **per canonical lock-path** is taken before
the OS lock (ordering: in-process → OS), so threads serialise before touching the
OS flock. The distinct `.{name}.rotate.lock` rotation sentinel is retained and
never collapsed into the storage lock (order: rotation-outer → storage-inner).

### Failure policy — per intent

On lock unavailability (deadline elapsed under contention, or infrastructure
failure such as a read-only dir / NFS without flock):

- `merge_cookie_delta` (CAS, key-level safe) **fails open** — status quo;
  availability wins and the snapshot/delta CAS guards preserve correctness.
- Full-replace / RMW intents (`update_account_metadata`, `persist_minted_jar`,
  `write_master_token`) **fail closed**, raising `LockUnavailableError` — the
  documented replacement for the former `filelock.Timeout`. Failing open here
  could overwrite a concurrent CAS delta, the exact lost-update class this ADR
  makes unrepresentable.
- `clear_in_band_account` stays best-effort (swallows), matching the
  pre-refactor `filelock` OSError arm — the legacy reader still resolves the
  record.

### Permission contract

POSIX: parent directory `0700` on creation (only for directories the write
creates — pre-existing dirs are left untouched) and file `0600` (via
`atomic_write_json`'s default mode). Windows relies on `%USERPROFILE%` ACL
inheritance. This closes the master-token path's mode-less `mkdir(parents=True)`
gap.

### Value-free outcomes

The v0.x `WriteOutcome` carries only an enum status — never cookie values, jars,
state dicts, or caught exceptions — so it is always safe to `repr`/log. First-party
browser capture consumes the equally value-free native `ReplaceResult`; the legacy
outcome is projected only inside the compatibility wrapper.

### Save-ordering ("close() must win", per client instance)

`CookiePersistence._save_v0_callback()` stamps each explicit callback dispatch from
`itertools.count()`
(`__next__` is GIL-atomic — the fix does not rest on the one-loop-per-client
contract). Under the save lock a worker drops itself if its sequence is older
than the newest sequence that already applied a merge to the same effective
path. The per-path marker advances only after an apply that actually ran the
merge (success **or** CAS-partial `ok=False` *with* rejected keys); a hard-fail
(`ok=False` *without* rejected keys) does not advance, so the older worker still
proceeds — its CAS-guarded write is strictly newer than disk. Direct
`AuthTokens.save_cookies` / `fetch_tokens_*` writers remain CAS-ordered only.

### Enforcement

An AST guardrail (`tests/_guardrails/test_storage_writer_boundary.py`) enforces
the boundary by construction: no `_auth` module outside the writer imports a
write primitive (except annotated, shrinking exemptions), dependency-seam
bindings are allowlisted, write-primitive calls on a `storage_state.json`
literal are forbidden, and an **equality-asserted** frozenset enumerates every
module repo-wide that imports `atomic_write_json` (so a new importer is loud).

### Runtime rejection + module-private bypass (b-PR3)

Once every storage-state caller is migrated onto the writer, the public
`atomic_write_json` (`notebooklm.io`) **rejects `storage_state.json` paths** with
`ValueError` — the same guard `atomic_update_json` has carried since #1215,
generalised. This is a **documented public-surface change** (precedent #1215): a
bare atomic write on `storage_state.json` skips the canonical dotted
`.storage_state.json.lock` sentinel and would re-open the lost-update race, so it
is refused. The canonical writer legitimately writes storage-state files under
that lock, so it uses a **module-private bypass** — `_atomic_io._atomic_write_json_unchecked`,
a private symbol (not a keyword flag) — which skips the guard. The AST guardrail
adds an equality-asserted allowlist pinning the bypass's importer set to exactly
`{storage_writer.py}`, and the b-PR3 storage-state-exemption set shrinks to
`{migration.py}`. Landing the rejection only after b-PR3 migrates the last
storage-state callers (the three CLI login/import writers and browser capture)
avoids breaking `notebooklm login` / `auth import-cookies` / browser capture
during the migration window.

### Login / import full-replace intent (b-PR3)

The native `profile_migration.replace_profile_from_login` operation is the sanctioned persist for
the CLI `login --browser-cookies`, `auth refresh --browser-cookies`, and
`auth import-cookies` flows. The v0.x `storage.replace_from_login` wrapper delegates to that
operation and preserves its old signature and result. Under the storage lock the native path applies the write-time
domain filter, re-validates `MINIMUM_REQUIRED_COOKIES` on the FILTERED state
(returning a value-free `ReplaceResult`; the wrapper projects this to the historical
`LoginWriteOutcome` and #2086 failure contract), embeds/clears the in-band
`notebooklm.account` binding through a primitive keep/clear/set directive (the compatibility
wrapper translates `KEEP_ACCOUNT` | `CLEAR_ACCOUNT` | `AccountRecord`),
records the `include_domains` opt-in set in the namespace, and (import flavour)
takes the pre-overwrite `.bak` backup INSIDE the lock.

*Amended (master-token relocation, #2103 PR-0):* the legacy sibling
`context.json[account]` cleanup (`_drop_legacy_account_key`, a different file
under a different lock) is now called by `replace_from_login` itself, at the
end of its own write, rather than by the CLI after `replace_from_login`
returns — the two remaining call sites (`cookie_writes.py`, `refresh.py`)
duplicated a scrub the writer can just as well own, and a scrub living outside
the writer is a scrub a future writer path can forget. The public read
fallback this scrub complemented is gone too: `read_account_metadata` is now
in-band-only (a standing legacy read on every call was a silent
wrong-account hazard — a missed `authuser` routes requests to a *different*
signed-in Google account) — but the reader still *heals* a legacy profile
rather than ignoring it: `read_account_metadata` calls a one-shot
`promote_legacy_account` migration whenever in-band is absent, so the result
is always genuinely in-band, migrated durably on first read rather than
re-derived from an unmigrated file every call. The startup profiles migration
also promotes proactively, as a completeness nicety (not a correctness
requirement — the reader retries regardless). `drop_legacy_account_key` is
consequently no longer imported by any
first-party caller; it remains importable from `notebooklm.auth` for
back-compat (de-blessed, not removed). `replace_from_login` / `LoginWriteOutcome`
/ `AccountRecord` / `KEEP_ACCOUNT` / `CLEAR_ACCOUNT` remain importable compatibility aliases.
First-party CLI code instead reaches the internal-ledger aliases `replace_profile_from_login` and
`ReplaceResult` through `notebooklm.auth`; neither is added to public `__all__`.

*Amended again (auth-deepening PR 5.1, ADR-0033):* the anti-wrong-account
contract above is unchanged, but it is no longer implemented by writing on a
read. `read_account_metadata` sits on the **per-RPC** token-route path
(`refresh._resolve_token_route_kwargs` → `get_authuser_for_storage`), and the
paragraph above had it taking the storage **write** lock — a 90 s bounded
acquire — on every read of a not-yet-migrated profile. The two compensations
that existed only for that (a promotion-specific 2 s lock deadline, and a
warn-once-per-path throttle so a persistently failing promotion would not log
twice per request forever) are both deleted with it.

What replaces it keeps the property that mattered: when in-band is absent the
reader **derives** the record read-only from the legacy sibling, through the
same `_sanitize_legacy_account_record` the promotion embeds with, so the result
is still genuinely in-band-*shaped* and never a raw legacy pass-through.
Durable promotion becomes a fire-and-forget one-shot per canonical storage
path, scheduled from the read and joined by nobody
(`account._schedule_legacy_promotion`). Consequences:

- The read takes **no** lock on either fast path, and takes only a brief
  scheduling lock — never the storage lock — on the legacy branch. The write
  never runs on a caller's thread.
- The one-shot is single-flight per path (N concurrent readers ⇒ one
  promotion) and does **not** retry in-process. A failed promotion costs
  nothing: the reader already answers correctly without it, so retrying would
  only put a failing write back near a per-RPC path.
- It hangs off the **read path**, not startup, deliberately. `migration.py`
  fires only for pre-v0.5.0 two-file HOME layouts and is not a general
  durable-promotion backstop, and the `NOTEBOOKLM_AUTH_JSON` env-auth path
  (#2083) may never pass through it at all — env-auth carries its record
  in-band by construction and has no sibling to promote.
- `replace_from_login`'s own `promote_legacy_account` call (the
  `KEEP_ACCOUNT`-with-no-in-band-record arm, which stops `auth import-cookies`
  from destroying a legacy profile's only binding) is untouched: it is a
  data-loss guard, not a migration backstop, and keeps its semantics.
- Observable delta for operators: on a legacy profile the sibling
  `context.json[account]` is scrubbed a moment *after* the first read rather
  than during it, and a profile whose promotion keeps failing is now warned
  about by a plain, default-visible WARNING rather than one gated behind a
  per-path throttle. The frequency is unchanged in the worst case and lower in
  practice: the one-shot is single-flight per canonical path and never retries
  in-process, so the read path can emit this at most once per path per process
  (plus at most one each from startup migration and `replace_from_login`). No
  returned value changes — `tests/unit/test_auth_account_promotion.py` pins
  derived-vs-promoted equality field-by-field across a matrix of malformed
  legacy shapes.

*Amended again (self-healing reconciliation, #2228):* the process-lifetime
"does not retry" rule above is removed. It made a transient 90-second storage
lock failure permanent until process exit and, more seriously, left
`context.json[account]` at rest forever when a process stopped after the
in-band write but before the sibling scrub: subsequent reads saw in-band and
never scheduled the one-shot again. The scheduler now deduplicates only an
**active** worker. After that worker settles, a later read may retry; concurrent
readers still share one worker and no storage or context lock runs on their
thread. An in-band resolution also checks for the stale sibling and schedules
the idempotent only-if-absent promote-or-scrub operation. Thus the 30-second
exit drain stays an observable ceiling rather than a false durability promise:
work legitimately queued behind the 90-second storage lock can outlive it, but
the next read or process repairs both the durable and privacy halves.

## Consequences

- All `storage_state.json` mutations funnel through one auditable module.
- The lost-update, non-durable-write, and save-reordering classes become
  unrepresentable for the migrated writers.
- The account/master-token writers' worst-case wait widens from 10 s to 90 s and
  their lock-failure exception type changes from `filelock.Timeout` to
  `LockUnavailableError` (callers' except-arms updated accordingly).
- The CAS merge keeps its status-quo fail-open acquire and its 51-test suite
  passes unmodified via the delegate seam. The two intended behavioural additions
  are the save-ordering guard ([storage-F3], §b.3 — in b-PR1's scope, not a pure
  relocation) and the lock exception-type/bound change above.

## Alternatives considered

- **Keep `atomic_write_json` everywhere, enforce the boundary with a lint rule
  only.** Rejected: a lint catches new *call sites*, but the durability, locking,
  and save-ordering policy would still live re-implemented at each writer, so the
  lost-update / non-durable / reorder classes stay *representable* — a reviewer
  mistake reopens them. Funnelling through one module makes them unrepresentable
  by construction; the AST guardrail is then a backstop, not the sole defense.
- **Per-call-site locked writers (no shared module).** Rejected: every login /
  import / re-mint / account path would re-derive the canonical dotted lock path
  and the acquire/fail policy, and the CAS-vs-full-file fail-open/closed split
  would drift between copies. One module gives a single audited home for the
  intent-shaped API and its per-intent policy.
- **A `StorageWriter` class instead of a module of intents.** Rejected: the
  writers are stateless and process-global (the lock lives on disk, the epoch in
  `single_flight`), so an instance adds lifecycle/wiring with no state to own; a
  module of intent functions matches the existing `_auth/` seam style and keeps
  `storage.save_cookies_to_storage` available as a direct v0.x facade.
- **Reuse `filelock` for the unified lock.** Rejected in favour of the
  project-internal `storage._file_lock` primitive (ADR-0029 lock unification): it
  shares one bounded-acquire deadline/backoff with the Windows `msvcrt` path and
  the dotted `.storage_state.json.lock` sentinel, avoiding a divergent lock file.

## Related references

- [Architecture](../architecture.md) — layered design and the `_auth/` file index.
- [ADR-0033](0033-auth-consolidation-policy.md) — the persistence merge that relocated this
  writer into `_auth/storage.py` and moved the boundary to function granularity.
- ADR-0017 — public facade / private implementation (the delegate-seam pattern).
- #1215 — `atomic_update_json` storage-state rejection (the enforced-invariant precedent).
