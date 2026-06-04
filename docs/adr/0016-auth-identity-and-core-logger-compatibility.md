# ADR-0016: Auth Identity and Core Logger Compatibility

## Status

Accepted.

## Context

The session-elimination work moved runtime ownership into
`NotebookLMClient.__init__`, `ClientComposed`, and direct collaborators. Two
historical invariants remain load-bearing after that move:

- `NotebookLMClient` and every auth-consuming collaborator must observe the
  same mutable `AuthTokens` instance. Auth refresh mutates token fields in
  place, so replacing the object for one consumer would strand the others on
  stale credentials.
- The deleted `_core.py` module still has a logging compatibility footprint:
  downstream tests and user filters match logger name `"notebooklm._core"`.
  Logger names are strings, not imports, so deleting the module did not make
  the string safe to rename.

Both rules were previously explained partly in shipped source comments and
partly in temporary implementation-plan notes. That made future maintenance
fragile because the source had no stable rationale to cite.

## Decision

**Auth Instance Invariant.** `NotebookLMClient` keeps one authoritative
`AuthTokens` object at
`self._auth`, and all collaborators that need auth observe that object rather
than a copied replacement. Refresh paths update the same object in place.

`CORE_LOGGER_NAME` remains the literal string `"notebooklm._core"` even though
there is no active `notebooklm._core` compatibility module. Treat the string as
a public-ish logging contract for filters, `caplog` assertions, and downstream
diagnostics.

Source comments that need to explain either invariant should cite this ADR
instead of temporary planning files.

## Consequences

Auth refresh remains identity-sensitive: construction may normalize
`storage_path` by rebinding the incoming `AuthTokens` before collaborator
construction, but once `self._auth` is set, runtime collaborators must alias
that object. Tests that validate auth propagation should assert identity where
identity is the contract, not only value equality.

Logging refactors must not rename the core logger casually. A future rename
requires a compatibility plan that preserves or deliberately deprecates filters
using `"notebooklm._core"`; ordinary module relocation is not enough rationale.

The ADR is intentionally narrow. It does not revive deleted compatibility
modules, and it does not make `_core` an active import path.

## Alternatives considered

**Let each collaborator own an `AuthTokens` copy.** Rejected because auth
refresh mutates credentials in place. Copies would make refresh visibility
depend on which collaborator held which instance, creating stale-cookie and
stale-CSRF bugs under concurrent use.

**Rename the logger to match the new module layout.** Rejected because the
logger name has become an observable behavior independent of the deleted
module. Renaming it would silently disable existing filters and test captures
without improving runtime isolation.

**Leave the rationale only in source comments.** Rejected because the comments
need a stable canonical home to cite. The invariant crosses multiple modules
and should survive future source reshuffles.
