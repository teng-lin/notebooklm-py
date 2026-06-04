# ADR-0018: Deprecation strategy (`_deprecation.py` + `MappingCompatMixin`)

## Status

Accepted (retroactive).

## Context

`notebooklm-py` evolves its public API: return shapes move from loose
`dict[str, Any]` mappings to typed dataclasses (issue #1209), keyword
arguments are renamed (e.g. `ResearchAPI.wait_for_completion`'s `interval` →
`initial_interval`), and accessor semantics tighten (`get()` returning `None`
on a miss will eventually raise a `*NotFoundError`, issue #1247). Each is a
breaking change for some callers.

Shipping breaking changes with no runway strips downstream users of any chance
to migrate; carrying every old behavior forever ossifies the API. The project
needs a *single, consistent* way to (a) keep old behavior working for a
deprecation window, (b) warn callers loudly enough to migrate, and (c) stay
silent for tests and scripts that have already opted in. Without a single home
for this, `warnings.warn(...)` calls scatter across feature modules with
inconsistent messages and no shared suppression switch.

The mechanics already live in `src/notebooklm/_deprecation.py`; this ADR
records the decision so the non-obvious versioning consequences are explicit.

## Decision

Centralize all deprecation mechanics in `src/notebooklm/_deprecation.py`, gate
**every** deprecation warning behind the `NOTEBOOKLM_QUIET_DEPRECATIONS`
environment variable, and name a concrete removal version (**v0.8.0** for the
current batch) in every message. The module provides three reusable
mechanisms:

1. **`warn_get_returns_none(resource, *, removal="0.8.0")`** — the single place
   that emits the `get()`-returns-`None` `DeprecationWarning`. Public
   `sources/artifacts/notes.get()` warn on a miss; the private `_get_or_none()`
   body never warns. The planned end state (issue #1247) is to raise
   `*NotFoundError` in v0.8.0.

2. **`deprecated_kwarg(...)`** — a keyword-alias helper that maps an old
   keyword to its replacement, warns naming the removal version, and raises
   when a caller passes both the old and new keyword. Used by
   `ResearchAPI.wait_for_completion` (`interval` → `initial_interval`).

3. **`MappingCompatMixin`** (defined at `_deprecation.py:221`) — a
   dict-subscript backward-compat bridge for dataclasses that replaced
   `dict[str, Any]` returns (issue #1209). Mixed into
   `ResearchTask`/`ResearchStart`/`MindMapResult`/`SourceGuide` and the
   mind-map value types under `_types/`, it makes `result["key"]` **warn** and
   return the value from the dataclass's `to_public_dict()`, while the
   mapping-style reads (`get`/`keys`/`__contains__`/`__iter__`) stay **silent**
   so defensive callers migrate without noise.

The rules:

1. **One module, one switch.** All deprecation warnings live in
   `_deprecation.py` and are silenced by `NOTEBOOKLM_QUIET_DEPRECATIONS`. No
   ad-hoc `warnings.warn(...)` scattered through feature modules.
2. **Name the removal version.** Every message states the version in which the
   old behavior is removed (v0.8.0 for the current batch).
3. **Warn at the boundary, not in the core.** Public methods warn; private
   helpers (e.g. `_get_or_none()`) already implement the future behavior
   without warning, so the eventual removal is a small, localized swap.
4. **Subscript warns, mapping reads stay quiet.** `MappingCompatMixin` warns
   only on `__getitem__`; `get`/`in`/iteration bridge silently.

## Consequences

**Wanted**

- **Predictable runway**: callers get a named version and a consistent warning
  shape for every deprecation.
- **Quiet for the already-migrated**: `NOTEBOOKLM_QUIET_DEPRECATIONS` lets
  tests and scripts opt out without monkeypatching `warnings`.
- **Low-cost removal**: because the future behavior already lives in private
  helpers, dropping a deprecation in v0.8.0 is a small, localized edit.

**Unwanted**

- **Version coupling**: the "v0.8.0" target is repeated across messages and
  docs; bumping it requires a coordinated sweep.
- **Two code paths during the window**: each active deprecation keeps both the
  old and new behavior alive until removal, adding temporary surface area.
- **Discipline required**: the single-module rule only holds if contributors
  route new deprecations through `_deprecation.py` rather than inline warnings.

## Alternatives considered

- **Clean breaks with no deprecation window.** Rejected for real public
  contracts: downstream callers need a runway, and the API-compat gate
  (ADR-0017 / `scripts/audit_public_api_compat.py`) exists precisely to stop
  unannounced breaks. (Clean breaks remain acceptable for non-contract
  internals and bug fixes — that is a separate policy, not a deprecation.)
- **Per-feature `warnings.warn(...)` calls.** Rejected. It produces
  inconsistent messages, no shared removal-version vocabulary, and no single
  suppression switch — exactly the fragmentation this ADR prevents.
- **Make `MappingCompatMixin` warn on every mapping access.** Rejected.
  Warning on `get`/`in`/iteration would flood defensive callers who are
  already forward-compatible; warning only on `__getitem__` targets the access
  pattern that actually breaks when the dict return becomes a dataclass.
