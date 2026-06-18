# Architecture Decision Records

This directory holds the canonical decisions that shape the `notebooklm-py` codebase. Each record explains _why_ a load-bearing pattern exists so that future contributors don't re-litigate (or silently re-introduce) the trade-off.

## How to use this directory

- Read the relevant ADR before changing the pattern it describes. If you disagree, write a new ADR that supersedes it — do not edit the original past correcting typos.
- Numbering is append-only. Retired ADR numbers are never re-used.
- Filenames follow `NNNN-short-title.md` (lowercase, kebab-case).
- Status values: `Proposed`, `Proposed — <short explanation>`, `Accepted`, `Accepted (retroactive)`, `Accepted (#PR)`, `Accepted (Sunset = <event>)`, `Superseded — <short explanation>`, `Superseded by ADR-NNNN (#PR)`, `Deprecated`, `Rejected`.
- Format: lightweight hybrid — six sections in this exact order: _Title heading_ (`# ADR-NNNN: <Title>`), _Status_, _Context_, _Decision_, _Consequences_, _Alternatives considered_. See [0000-template.md](0000-template.md).

## When an ADR is required

The pull-request template asks contributors to confirm that any change to the _architectural shape_ of the codebase carries an ADR addition or update. "Architectural shape" means any of:

- New / removed / relocated modules in `src/notebooklm/_runtime/`, `src/notebooklm/_middleware/`, `src/notebooklm/auth.py`, `src/notebooklm/_auth/`, `src/notebooklm/cli/services/`.
- Changes to the contracts between layers (CLI ↔ Client ↔ Core ↔ RPC).
- New or retired test patterns (fixtures, monkeypatch policy, conformance tests).
- New cross-cutting policies (retry, idempotency, scrubbing, loop affinity).

Pure bug fixes, additive RPC method IDs, and CLI ergonomics changes do not require an ADR.

### Status Format Legend

The ADR Index table utilizes five eras of Status notation to reflect the lifecycle of decisions:

1. **`Accepted` / `Accepted (retroactive)`** — The decision is currently active and adopted.
2. **`Accepted (Tier X PR Y.Z)`** — Historical PR-naming convention from the early refactoring tiers (Tiers 11-12).
3. **`Superseded — <note>` / `Superseded by ADR-NNNN (#PR)`** — Canonical supersession forms, either with a short inline explanation or a replacing ADR and PR.
4. **`Accepted (#PR)` / `Accepted; <note>` / `Proposed — <note>`** — Short explanatory forms used when a status needs one compact qualifier.
5. **`Superseded by <named-PR> (D1/D2 PR-X)`** — Pre-canonical historical supersession form, linking to specific branch-refactoring PRs.

## Index

| ADR                                                           | Title                                                         | Status                                                                                                              |
| ------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| [0001](0001-layered-core-seams-and-property-bridge-policy.md) | Layered `_core` seams and the property-bridge policy          | Superseded — bridge policy retired in session-shrink arc                                                            |
| [0002](0002-capability-protocol-pattern.md)                   | Capability Protocol pattern (`SessionCapabilities` fat union) | Superseded by [arch-d2-cutover](https://github.com/teng-lin/notebooklm-py/pull/835) (#835)                          |
| [0003](0003-auth-facade-write-through.md)                     | `auth.py` write-through facade (`_AuthFacadeModule`)          | Superseded — closed by [ADR-0014](0014-feature-local-runtime-adapters.md) (session-decoupling Waves 3a + 4 T2.2 + 5) |
| [0004](0004-loop-affinity-contract.md)                        | Loop-affinity contract for `NotebookLMClient`                 | Accepted (retroactive)                                                                                              |
| [0005](0005-idempotency-taxonomy.md)                          | Mutating-RPC idempotency taxonomy                             | Accepted (retroactive)                                                                                              |
| [0006](0006-vcr-scrubber-strategy.md)                         | VCR cassette scrubber strategy                                | Accepted (retroactive)                                                                                              |
| [0007](0007-test-monkeypatch-policy.md)                       | Test monkeypatch policy                                       | Accepted                                                                                                            |
| [0008](0008-cli-services-extraction-pattern.md)               | `cli/services/` extraction pattern                            | Accepted (retroactive)                                                                                              |
| [0009](0009-middleware-chain.md)                              | Middleware chain for cross-cutting transport concerns         | Accepted (Tier 12 PR 12.1); context refined by [ADR-0013](0013-composable-session-capabilities.md) (#866)            |
| [0010](0010-session-kernel-split.md)                          | Session/Kernel split                                          | Superseded by [ADR-0013](0013-composable-session-capabilities.md) (#866)                                             |
| [0011](0011-schema-validation-policy.md)                      | Schema validation policy (strict-decode default)              | Accepted (Tier 13 PR 13.9a)                                                                                         |
| [0012](0012-implementation-surface-convention.md)             | Implementation surface convention (underscore-prefix policy)  | Accepted (Tier 13 PR 13.9a)                                                                                         |
| [0013](0013-composable-session-capabilities.md)               | Composable Session Capabilities and Feature-Local Runtimes    | Accepted                                                                                                            |
| [0014](0014-feature-local-runtime-adapters.md)                | Feature-local runtime adapters as Protocol satisfiers         | Accepted (#1082)                                                                                                    |
| [0015](0015-json-envelope-contract-for-post-parse-click-exceptions.md) | Typed JSON error envelope covers post-parse `ClickException` failures | Accepted                                                                                                            |
| [0016](0016-auth-identity-and-core-logger-compatibility.md) | Auth identity and core logger compatibility | Accepted                                                                                                            |
| [0017](0017-public-facade-private-implementation.md) | Public-facade / private-implementation re-export convention | Accepted (retroactive) |
| [0018](0018-deprecation-strategy.md) | Deprecation strategy (`_deprecation.py`) | Accepted (retroactive) |
| [0019](0019-error-and-return-contract.md) | Error-and-return contract for the public API | Accepted; v0.8.0 implementation landed |
| [0020](0020-sealed-async-result-types.md) | Sealed async result types for artifact generation | Proposed — design of record; recommends continued deferral (ADR-0019 Tier 3 / #1345) |
| [0021](0021-transport-neutral-app-layer.md) | Transport-neutral application layer (`_app/`) | Accepted |
| [0022](0022-regenerable-baselines.md) | Regenerable test baselines (derive / store / compare / regen) | Accepted |

ADR-0007 ships alongside its enforcement substrate: the concrete fixtures (`tests/_fixtures/`) and meta-lint (`tests/_guardrails/test_no_forbidden_monkeypatches.py`) are added in the same PR (`arch-d1-fixtures-scaffolding`) so the record is grounded in working code rather than an empty placeholder.

## Related references

- [Architecture](../architecture.md) — Canonical overview of the layered architecture.
- `docs/development.md` — contributor-facing process notes (testing, releasing, environment setup).
- `CLAUDE.md` — onboarding map for AI assistants. Architectural rationale belongs here in `docs/adr/`, not in `CLAUDE.md`.
