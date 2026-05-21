# Architecture Decision Records

This directory holds the canonical decisions that shape the `notebooklm-py` codebase. Each record explains *why* a load-bearing pattern exists so that future contributors don't re-litigate (or silently re-introduce) the trade-off.

## How to use this directory

- Read the relevant ADR before changing the pattern it describes. If you disagree, write a new ADR that supersedes it — do not edit the original past correcting typos.
- Numbering is append-only. Retired ADR numbers are never re-used.
- Filenames follow `NNNN-short-title.md` (lowercase, kebab-case).
- Status values: `Proposed`, `Accepted`, `Accepted (Sunset = <event>)`, `Superseded by ADR-NNN (#PR)`, `Deprecated`, `Rejected`.
- Format: lightweight hybrid — six sections in this exact order: *Title heading* (`# ADR-NNN: <Title>`), *Status*, *Context*, *Decision*, *Consequences*, *Alternatives considered*. See [0000-template.md](0000-template.md).

## When an ADR is required

The pull-request template asks contributors to confirm that any change to the *architectural shape* of the codebase carries an ADR addition or update. "Architectural shape" means any of:

- New / removed / relocated modules in `src/notebooklm/_core*.py`, `src/notebooklm/_capabilities.py`, `src/notebooklm/auth.py`, `src/notebooklm/_auth/`, `src/notebooklm/cli/services/`.
- Changes to the contracts between layers (CLI ↔ Client ↔ Core ↔ RPC).
- New or retired test patterns (fixtures, monkeypatch policy, conformance tests).
- New cross-cutting policies (retry, idempotency, scrubbing, loop affinity).

Pure bug fixes, additive RPC method IDs, and CLI ergonomics changes do not require an ADR.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-layered-core-seams-and-property-bridge-policy.md) | Layered `_core` seams and the property-bridge policy | Accepted (retroactive) |
| [0002](0002-capability-protocol-pattern.md) | Capability Protocol pattern (`SessionCapabilities` fat union) | Superseded by the `arch-d2-cutover` PR (D2 PR-2) |
| [0003](0003-auth-facade-write-through.md) | `auth.py` write-through facade (`_AuthFacadeModule`) | Superseded by `arch-d1-auth-side` (D1 PR-2) |
| [0004](0004-loop-affinity-contract.md) | Loop-affinity contract for `NotebookLMClient` | Accepted (retroactive) |
| [0005](0005-idempotency-taxonomy.md) | Mutating-RPC idempotency taxonomy | Accepted (retroactive) |
| [0006](0006-vcr-scrubber-strategy.md) | VCR cassette scrubber strategy | Accepted (retroactive) |
| [0007](0007-test-monkeypatch-policy.md) | Test monkeypatch policy | Accepted |
| [0008](0008-cli-services-extraction-pattern.md) | `cli/services/` extraction pattern | Accepted (retroactive) |
| [0009](0009-middleware-chain.md) | Middleware chain for cross-cutting transport concerns | Accepted (Tier 12 PR 12.1) |
| [0010](0010-session-kernel-split.md) | Session/Kernel split | Superseded by ADR-013 (#866) |
| [0011](0011-schema-validation-policy.md) | Schema validation policy (strict-decode default) | Accepted (Tier 13 PR 13.9a) |
| [0012](0012-implementation-surface-convention.md) | Implementation surface convention (underscore-prefix policy) | Accepted (Tier 13 PR 13.9a) |
| [0013](0013-composable-session-capabilities.md) | Composable Session Capabilities and Feature-Local Runtimes | Accepted |

ADR-007 ships alongside its enforcement substrate: the concrete fixtures (`tests/_fixtures/`) and meta-lint (`tests/_lint/test_no_forbidden_monkeypatches.py`) are added in the same PR (`arch-d1-fixtures-scaffolding`) so the record is grounded in working code rather than an empty placeholder.

## Related references

- `docs/architecture-evolution.md` — BEFORE / AFTER / GREENFIELD diagrams that visualise the disease state and the post-remediation target.
- `docs/architecture-post-remediation.md` — ASCII view of the layered architecture after the D3 → D2 → D1 remediation arc lands.
- `docs/development.md` — contributor-facing process notes (testing, releasing, environment setup).
- `CLAUDE.md` — onboarding map for AI assistants. Architectural rationale belongs here in `docs/adr/`, not in `CLAUDE.md`.
