# ADR-0029: Single canonical `storage_state.json` writer

## Status

Accepted (rolling out — refactor (b): b-PR1 lands the writer + guardrail; b-PR2
migrates browser capture / L4 re-mint; b-PR3 migrates the three CLI login/import
writers onto `replace_from_login`, lands the runtime `atomic_write_json`
storage-state rejection + module-private bypass, and shrinks the storage-state
exemption to `{migration.py}`).

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
  (L4 re-mint persist + `master_token.json`), `_auth/browser_capture.py`, and
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

### Patch-seam continuity

`_auth/storage.py` keeps `save_cookies_to_storage` as the importable,
monkeypatchable delegate that forwards to `storage_writer.merge_cookie_delta`
(the pattern of ADR-0017). `_runtime/lifecycle.py` late-binds it and ~18 test
files patch it, so the seam does not move. The CAS math helpers
(`_merge_cookies_with_snapshot`, snapshot/baseline helpers, `CookieSaveResult`)
and the `_file_lock` primitive stay in `storage.py`; the writer imports them.

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

`WriteOutcome` carries only an enum status — never cookie values, jars, state
dicts, or caught exceptions — so it is always safe to `repr`/log.

### Save-ordering ("close() must win", per client instance)

`CookiePersistence.save()` stamps each dispatch from `itertools.count()`
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

`replace_from_login` is the single sanctioned persist for the CLI
`login --browser-cookies`, `auth refresh --browser-cookies`, and
`auth import-cookies` flows. Under the storage lock it applies the write-time
domain filter, re-validates `MINIMUM_REQUIRED_COOKIES` on the FILTERED state
(returning a value-free `LoginWriteOutcome(required_cookies_dropped, …)` — mapping
to #2086's `CookieValidationFailure(COOKIE_VALIDATION_FAILED)` / `io.fail(1)` /
not-exists contract), embeds/clears the in-band `notebooklm.account` binding via
an `account` sentinel (`KEEP_ACCOUNT` | `CLEAR_ACCOUNT` | `AccountRecord`),
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
/ `AccountRecord` / `KEEP_ACCOUNT` / `CLEAR_ACCOUNT` are additive
`notebooklm.auth` re-exports so the CLI reaches them without importing private
`_auth` modules.

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
  the delegate seam (`storage.save_cookies_to_storage`) monkeypatchable.
- **Reuse `filelock` for the unified lock.** Rejected in favour of the
  project-internal `storage._file_lock` primitive (ADR-0029 lock unification): it
  shares one bounded-acquire deadline/backoff with the Windows `msvcrt` path and
  the dotted `.storage_state.json.lock` sentinel, avoiding a divergent lock file.

## Related references

- [Architecture](../architecture.md) — layered design and the `_auth/` file index.
- ADR-0017 — public facade / private implementation (the delegate-seam pattern).
- #1215 — `atomic_update_json` storage-state rejection (the enforced-invariant precedent).
