# ADR-0018: Deprecation strategy (`_deprecation.py`)

> **Current state (2026-06-11).** The v0.8.0 runway described below completed:
> `warn_get_returns_none`, `deprecated_kwarg`, `MappingCompatMixin`, and the
> `NOTEBOOKLM_FUTURE_ERRORS` preview gate were removed when those breaking
> changes became default behavior. `_deprecation.py` now exposes the generic
> `warn_deprecated` helper plus the `NOTEBOOKLM_QUIET_DEPRECATIONS` suppression
> gate. The only currently scheduled public deprecation is awaiting
> `NotebookLMClient.from_storage(...)`; see `docs/deprecations.md`.

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

Centralize deprecation mechanics in `src/notebooklm/_deprecation.py`, gate
**every** project `DeprecationWarning` behind the
`NOTEBOOKLM_QUIET_DEPRECATIONS` environment variable, and name a concrete
removal version whenever one is scheduled. The surviving reusable mechanism is
`warn_deprecated(message, *, removal, stacklevel)`, used for one-off public
deprecations such as awaiting `NotebookLMClient.from_storage(...)`.

Historical helpers from the v0.7 → v0.8 migration have been retired:
`warn_get_returns_none`, `deprecated_kwarg`, and `MappingCompatMixin` no longer
exist because `get()` now raises, `interval=` is gone, and typed returns are
attribute-only dataclasses.

The rules:

1. **One module, one switch.** All deprecation warnings live in
   `_deprecation.py` and are silenced by `NOTEBOOKLM_QUIET_DEPRECATIONS`. No
   ad-hoc `warnings.warn(...)` scattered through feature modules.
2. **Name the removal version when scheduled.** Every message states the version
   in which the old behavior is removed when one is known. Pass
   `removal=None` only for unscheduled deprecations or non-deprecation runtime
   warnings that deliberately do not route through `_deprecation.py`.
3. **Warn at the boundary, not in the core.** Public methods warn; private
   helpers (e.g. `_get_or_none()`) already implement the future behavior
   without warning, so the eventual removal is a small, localized swap.
4. **Retire runway helpers when the break ships.** Once a deprecation window
   closes, delete the compatibility helper instead of keeping dormant legacy
   behavior in `_deprecation.py`.

## Consequences

**Wanted**

- **Predictable runway**: callers get a named version and a consistent warning
  shape for every deprecation.
- **Quiet for the already-migrated**: `NOTEBOOKLM_QUIET_DEPRECATIONS` lets
  tests and scripts opt out without monkeypatching `warnings`.
- **Low-cost removal**: because the future behavior already lives in private
  helpers, dropping a deprecation is a small, localized edit.

**Unwanted**

- **Version coupling**: removal targets appear in messages and docs; bumping
  them requires a coordinated sweep.
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
- **Make `MappingCompatMixin` warn on every mapping access.** Rejected during
  the v0.7 runway. Warning on `get`/`in`/iteration would have flooded defensive
  callers who were already forward-compatible; warning only on `__getitem__`
  targeted the access pattern that actually broke when the dict return became a
  dataclass. The mixin has since been removed.
