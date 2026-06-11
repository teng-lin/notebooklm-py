# ADR-0017: Public-facade / private-implementation re-export convention

## Status

Accepted (retroactive).

## Context

`notebooklm-py` exposes a public Python API that downstream callers import and
depend on. Internally, the implementation is decomposed into many small,
underscore-prefixed modules — the underscore-prefix privacy policy is recorded
in [ADR-0012](0012-implementation-surface-convention.md), and the runtime
decomposition into narrow collaborators in
[ADR-0013](0013-composable-session-capabilities.md) /
[ADR-0014](0014-feature-local-runtime-adapters.md). These goals pull against
each other:

- Private modules are split, renamed, and merged frequently. The current
  module map in `docs/architecture.md` records moves such as the old
  `_session_contracts.py` home becoming `_runtime/contracts.py`.
- Public import paths must stay stable: a renamed module that a caller imported
  from is a breaking change.

ADR-0012 establishes *that* a module is private (the leading underscore). It
does not, on its own, say how a public symbol whose implementation lives in a
private module is exposed. Without a paired convention, either the public
surface ossifies the internal layout (blocking refactors) or a refactor
silently breaks callers who imported from a private module.

The codebase already resolves this with a consistent pattern, but it is
undocumented as a decision: short public modules that re-export from one or
more underscore-prefixed implementation modules. `auth.py` is the most
developed example — its body is almost pure re-exports forwarding to the
`_auth/` package (see [ADR-0003](0003-auth-facade-write-through.md) and the
`auth.py` row in `docs/architecture.md`).

## Decision

For each cohesive slice of the public API, a short, stable, **public** module
re-exports the symbols whose **implementation** lives in one or more
underscore-prefixed **private** modules. Callers import only from the public
facade; the private modules are free to move.

The established facade ↔ implementation pairs (verified against the current
source tree) are:

| Public facade | Private implementation |
| ------------- | ---------------------- |
| `types.py`    | `_types/` package      |
| `config.py`   | `_env.py`              |
| `urls.py`     | `_url_utils.py`        |
| `io.py`       | `_atomic_io.py`        |
| `log.py`      | `_logging.py`          |
| `research.py` | `_research_task_parser.py` (plus public helpers) |
| `auth.py`     | `_auth/` package       |

Single-purpose public helper modules (`artifacts.py`, `utils.py`,
`migration.py`) follow the same shape: a stable public name backed by private
collaborators.

The rules:

1. **Public modules are facades where practical.** A public module's body is
   usually re-exports (with at most a thin binding, e.g.
   `auth.enumerate_accounts`). Small public helper modules such as
   `artifacts.py`, `utils.py`, and `migration.py` may contain public helper
   logic directly when that is the stable API.
2. **Callers import from the facade.** Importing from an underscore-prefixed
   module is unsupported and may break without notice.
3. **Private modules may move freely.** Splitting, renaming, or merging a
   private module is *not* a breaking change as long as the facade's
   re-exports are preserved.
4. **The facade is the compatibility contract.** Removing or renaming a symbol
   re-exported by a public facade *is* a breaking change; it is gated by the
   API-compat allowlist (`scripts/audit_public_api_compat.py` /
   `scripts/api-compat-allowlist.json`) and follows the deprecation strategy in
   [ADR-0018](0018-deprecation-strategy.md).

## Consequences

**Wanted**

- **Refactor freedom**: internal decomposition (ADR-0012/013/014) proceeds
  without churning the public API.
- **One obvious import path**: callers have a single stable location per
  concept, documented in `docs/python-api.md`.
- **Mechanical compatibility checks**: because the public surface is a small,
  enumerable set of facades, `scripts/audit_public_api_compat.py` can diff it
  and fail the Code Quality gate on unapproved breaking changes.

**Unwanted**

- **Indirection**: reading a public module rarely shows the logic; you must
  follow the re-export into the private module.
- **Drift risk**: a facade can fall out of sync with its implementation if a
  symbol is added privately but not re-exported. The public API compatibility
  audit (`scripts/audit_public_api_compat.py --check-stale`) and public-surface
  guardrails are the compatibility checks; `docs/architecture.md` is the
  human-readable module map.
- **Two names for one thing**: every facade ↔ implementation pair is a small
  bookkeeping cost in the architecture/module map whenever a private module
  moves.

## Alternatives considered

- **Expose the underscore modules directly and let callers import them.**
  Rejected. It couples every caller to the internal layout; the frequent
  private-module renames recorded in `docs/architecture.md` would each become a breaking
  change.
- **One flat public module that contains the implementation.** Rejected. It
  forfeits the decomposition benefits of ADR-0013/014 (narrow, testable
  collaborators) and re-creates the god-module the runtime arc dismantled.
- **No convention — decide ad hoc per symbol.** Rejected. Inconsistency is
  exactly what makes the public surface hard to audit mechanically; a single
  convention is what lets `audit_public_api_compat.py` reason about the whole
  surface.
