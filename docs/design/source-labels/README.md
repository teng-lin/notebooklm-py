# Source Labels — design package

**Status:** Proposed — design only, **not implemented** in `src/notebooklm/`.

Design docs for NotebookLM's "Auto-label sources by topic" feature: AI groups a
notebook's sources into topic **labels** (many-to-many; a label owns a list of
source IDs, a source carries no back-reference). Public surface:
`client.labels.generate(scope="all"|"unlabeled")` (AI grouping) + `create()`
(manual), both over the multi-mode `CREATE_LABEL` RPC (`agX4Bc`); `list`/`get`,
`update`/`set_emoji`/`add_sources`, `delete`.

## Read in order (source-of-truth chain)

| Doc | Role | SoT for |
|-----|------|---------|
| [`rpc.md`](./rpc.md) | Reverse-engineered RPC capture (DevTools, 2026-06-06) | the wire protocol — request shapes, response nesting, confirmed/open behaviors |
| [`api.md`](./api.md) | Proposed API design (3 review rounds: Claude / Gemini / Codex) | the public Python + CLI surface, RPCMethod naming, idempotency classes |
| [`implementation-plan.md`](./implementation-plan.md) | Gate-exhaustive TDD execution plan | the build order + every hardcoded CI gate to edit |

Each doc cites the one above it as its source of truth; don't change `api.md`
behavior without reconciling against `rpc.md`.

## At implementation (plan Phase 4)

The durable content folds into the published reference docs and this package
becomes a historical design record:

- `rpc.md` → `docs/rpc-reference.md`
- `api.md` → `docs/python-api.md` + `docs/cli-reference.md`
- `implementation-plan.md` → retired once the work merges

## ADR posture

No new ADR — the feature is pattern-conformant (additive RPC IDs + a namespaced
API on the existing resource template). It applies ADR-0005 (idempotency),
ADR-0008 (`cli/services/` extraction), ADR-0012/0017 (impl surface / facade
re-exports), and ADR-0019 (error contract). See `api.md` §ADR and the plan's
ADR-posture note.
