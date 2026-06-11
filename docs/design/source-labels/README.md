# Source Labels — design package

**Status:** Historical design record — implemented in v0.8.0 and folded into the
published docs.

Historical design docs for NotebookLM's "Auto-label sources by topic" feature:
AI groups a
notebook's sources into topic **labels** (many-to-many; a label owns a list of
source IDs, a source carries no back-reference). Public surface:
`client.labels.generate(scope="all"|"unlabeled")` (AI grouping) + `create()`
(manual), both over the multi-mode `CREATE_LABEL` RPC (`agX4Bc`); `list`/`get`,
`get_or_none`/`sources`, `update`/`rename`/`set_emoji`, `add_sources`,
`remove_sources`, and `delete`.

## Read in order (source-of-truth chain)

| Doc | Role | SoT for |
|-----|------|---------|
| [`rpc.md`](./rpc.md) | Reverse-engineered RPC capture (DevTools, 2026-06-06/07) | the wire protocol — request shapes, response nesting, confirmed/open behaviors |
| [`api.md`](./api.md) | Historical API design (3 review rounds: Claude / Gemini / Codex) | the shipped Python + CLI surface, RPCMethod naming, idempotency classes |
| [`implementation-plan.md`](./implementation-plan.md) | Historical gate-exhaustive TDD execution plan | the build order + hardcoded CI gates that were touched |

Each doc cites the one above it as its source of truth; don't change `api.md`
behavior without reconciling against `rpc.md`.

## Folded into published docs

The durable content has been folded into the published reference docs:

- `rpc.md` → `docs/rpc-reference.md`
- `api.md` → `docs/python-api.md` + `docs/cli-reference.md`
- `implementation-plan.md` -> historical execution record

## ADR posture

No new ADR — the feature is pattern-conformant (additive RPC IDs + a namespaced
API on the existing resource template). It applies ADR-0005 (idempotency),
ADR-0008 (`cli/services/` extraction), ADR-0012/0017 (impl surface / facade
re-exports), and ADR-0019 (error contract). See `api.md` section 9 and the
plan's ADR-posture note.
