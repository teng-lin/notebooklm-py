# Architecture diagram catalog

These diagrams are the visual companion to [`docs/architecture.md`](../architecture.md). After the
first successful GitHub Pages deployment, each **Explore** link opens the hosted viewer with
light/dark themes, guided focus modes, search, and export. Pages reflects the latest successful
deployment from `main`; the adjacent **Source** link opens the editable JSON source in GitHub. Do
not hand-edit generated HTML.

## Hosting

The [`pages.yml`](../../.github/workflows/pages.yml) workflow publishes only the numbered HTML
viewers after changes reach `main`; pull requests never deploy over the public site. A repository
administrator must select **Settings → Pages → Build and deployment → Source: GitHub Actions** once
before the first deployment. Until that deployment succeeds, or if Pages is temporarily
unavailable, open the committed HTML viewer from a local checkout. The workflow also serves the
system overview at the [Pages site root](https://teng-lin.github.io/notebooklm-py/).

## System and boundaries

| # | Diagram | What it answers | Files |
| --- | --- | --- | --- |
| 01 | System overview | How do library calls and the CLI, MCP, and REST adapters reach the two backends? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/01-system-overview.html) · [Source](./01-system-overview.architecture.json) |
| 02 | Adapters and application layer | Which responsibilities belong to frontend adapters and `_app/`? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/02-adapters-and-app-layer.html) · [Source](./02-adapters-and-app-layer.architecture.json) |
| 03 | Client runtime and transport | How do SharedRuntime and the selected backend bundle own dispatch, transport, and compatibility? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/03-client-runtime-and-transport.html) · [Source](./03-client-runtime-and-transport.architecture.json) |
| 05 | Feature services | Which stateful services sit behind sources, artifacts, and chat? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/05-feature-services.html) · [Source](./05-feature-services.architecture.json) |
| 06 | Web and Android backends | Which mechanisms are neutral, and where do WebRuntime and AndroidRuntime split? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/06-backends-web-and-android.html) · [Source](./06-backends-web-and-android.architecture.json) |
| 22 | Tests and guardrails | Which suites and parity, boundary, and scope-hook gates protect each layer? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/22-testing-and-guardrails.html) · [Source](./22-testing-and-guardrails.architecture.json) |
| 23 | Runtime class model | How do SharedRuntime, WebRuntime, AndroidRuntime, and the lazy sidecar relate? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/23-runtime-class-model.html) · [Source](./23-runtime-class-model.architecture.json) |
| 27 | Capability contracts | Which implementations satisfy the RPC, loop, and single-consumer contracts? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/27-capability-contracts.html) · [Source](./27-capability-contracts.architecture.json) |
| 28 | Profile, auth, and backend selection | Which auth, runtime, raw adapter, and compatibility resources does each backend construct? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/28-profile-auth-backend-selection.workflow.html) · [Source](./28-profile-auth-backend-selection.workflow.json) |
| 29 | Organization and sharing | Which APIs span account and notebook scope, and where do sharing and membership live? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/29-organization-and-sharing.architecture.html) · [Source](./29-organization-and-sharing.architecture.json) |
| 30 | Transfer security boundaries | How do the Web and Android transfer planes fence URLs, credentials, cleanup, and publication? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/30-transfer-security-boundaries.dataflow.html) · [Source](./30-transfer-security-boundaries.dataflow.json) |
| 34 | Client ownership boundaries | Which resources belong to configuration, construction, one client instance, or one operation? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/34-client-ownership-boundaries.html) · [Source](./34-client-ownership-boundaries.architecture.json) |
| 35 | Client construction and lifecycle | How do build, open, drain, close, and reopen preserve one-owner resource rules? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/35-client-construction-and-lifecycle.html) · [Source](./35-client-construction-and-lifecycle.lifecycle.json) |

## Authentication

| # | Diagram | What it answers | Files |
| --- | --- | --- | --- |
| 04 | Authentication | How does refresh dispatch to Web cookie recovery or Android bearer re-minting? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/04-authentication.html) · [Source](./04-authentication.architecture.json) |
| 10 | Login workflow | How do interactive login, browser-cookie import, and master-token bootstrap differ? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/10-login-workflow.html) · [Source](./10-login-workflow.workflow.json) |
| 24 | Authentication class model | Which typed values, stores, and coordinators own credential state and recovery? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/24-auth-class-model.html) · [Source](./24-auth-class-model.architecture.json) |
| 31 | Cold authentication recovery | How does a cold Web profile recover through a refresh command, headless bootstrap, and path-serialized L4 re-minting? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/31-cold-auth-recovery.html) · [Source](./31-cold-auth-recovery.sequence.json) |

## Calls, data flows, and lifecycles

| # | Diagram | What it answers | Files |
| --- | --- | --- | --- |
| 07 | RPC call paths | How do Web, Android, and deprecated compatibility calls reach their selected runtime? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/07-rpc-call-path.html) · [Source](./07-rpc-call-path.sequence.json) |
| 11 | Chat ask sequence | Which component owns locks, streaming, conversation recovery, and result assembly? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/11-chat-ask-sequence.html) · [Source](./11-chat-ask-sequence.sequence.json) |
| 12 | Source ingest | How do file bytes and no-byte inputs become ready grounding sources? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/12-source-ingest-dataflow.html) · [Source](./12-source-ingest-dataflow.dataflow.json) |
| 13 | Artifact lifecycle | How does generation move through pending, complete, failed, and retry states? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/13-artifact-lifecycle.html) · [Source](./13-artifact-lifecycle.lifecycle.json) |
| 15 | Android call path | What happens between an Android namespace call and protobuf projection? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/15-android-call-path.html) · [Source](./15-android-call-path.sequence.json) |
| 19 | Client resource lifecycle | How do open, bind, drain, close, and reopen affect owned resources? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/19-client-resource-lifecycle.html) · [Source](./19-client-resource-lifecycle.lifecycle.json) |
| 20 | Retry policy | How do both backends preserve retry parity and surface ambiguous writes honestly? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/20-retry-policy-workflow.html) · [Source](./20-retry-policy-workflow.workflow.json) |
| 21 | Deep research lifecycle | How do research tasks progress through polling, completion, import, failure, or cancellation? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/21-deep-research-lifecycle.html) · [Source](./21-deep-research-lifecycle.lifecycle.json) |
| 32 | MCP client provider | How does the MCP adapter open, reuse, invalidate, and recover its shared client? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/32-mcp-client-provider.html) · [Source](./32-mcp-client-provider.sequence.json) |
| 33 | MCP detached chat task | How does a detached chat task move from creation through polling, terminal observation, and TTL expiry? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/33-mcp-detached-chat-task.html) · [Source](./33-mcp-detached-chat-task.lifecycle.json) |
| 36 | Operation deadline and cancellation | How does one operation budget span nested work while preserving caller cancellation? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/36-operation-deadline-and-cancellation.html) · [Source](./36-operation-deadline-and-cancellation.sequence.json) |
| 37 | Operation journal and recovery | How does attempt evidence become commit certainty and a safe recovery action? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/37-operation-journal-and-recovery.html) · [Source](./37-operation-journal-and-recovery.dataflow.json) |
| 38 | Adapter prepare, confirm, and execute | How does a non-mutating canonical-ID preview cross human delay before a separate confirmed execution? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/38-adapter-prepare-confirm-execute.html) · [Source](./38-adapter-prepare-confirm-execute.workflow.json) |

## Domain models and adapters

| # | Diagram | What it answers | Files |
| --- | --- | --- | --- |
| 08 | Artifacts class model | How do artifact contracts, services, polling, and public result types relate? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/08-artifacts-class-model.html) · [Source](./08-artifacts-class-model.architecture.json) |
| 09 | Exception hierarchy | Which public exceptions and download auth metadata can adapters classify? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/09-exception-hierarchy.html) · [Source](./09-exception-hierarchy.architecture.json) |
| 14 | Android subsystem | Which credentials, session, transfers, phenotype, retry, and epoch mechanisms compose AndroidRuntime? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/14-android-backend.html) · [Source](./14-android-backend.architecture.json) |
| 16 | CLI subsystem | How do Click commands, services, `_app/`, and renderers divide work? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/16-cli-subsystem.html) · [Source](./16-cli-subsystem.architecture.json) |
| 17 | MCP subsystem | How do MCP tools share the neutral core and mutation confirmation policy? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/17-mcp-subsystem.html) · [Source](./17-mcp-subsystem.architecture.json) |
| 18 | REST subsystem | How do routes, authentication, limits, pending state, and the client lifespan fit together? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/18-rest-server-subsystem.html) · [Source](./18-rest-server-subsystem.architecture.json) |
| 25 | Sources class model | How do source contracts, upload/add services, and public source values relate? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/25-sources-class-model.html) · [Source](./25-sources-class-model.architecture.json) |
| 26 | Chat, notes, and mind maps | How do the conversation and note-backed/interactive surfaces compose? | [Explore](https://teng-lin.github.io/notebooklm-py/diagrams/26-chat-notes-class-model.html) · [Source](./26-chat-notes-class-model.architecture.json) |

## Coverage policy

The catalog covers the runtime layers, both backend graphs, all eleven public namespaces, the
three frontend adapters, authentication, the main byte-transfer boundaries, and the workflows
whose ordering or state is difficult to understand from prose alone. The September 2026 audit
added diagrams 28–30 because backend/profile precedence, the five organization namespaces, and
per-hop transfer security were the material gaps in the original set. The branch-local assembler,
root compatibility-sidecar, shared source workflow, and guarded-transfer refactors refresh the
affected diagrams without adding external systems, RPC ids, or wire shapes.
Diagrams 31–33 add the previously missing cold-authentication recovery sequence, MCP provider
open/recovery sequence, and detached-task lifecycle, where concurrency and cleanup ordering matter.
Diagrams 34–38 document the client ownership model, construction and shutdown lifecycle,
whole-operation deadline and cancellation contract, mutation journal and recovery evidence, and
the adapter prepare/confirm/execute boundary introduced by the September 2026 ownership refactor.

Some views are deliberately not generated:

- The exact repository tree stays in [`architecture.md`](../architecture.md#file-map),
  where `scripts/check_claude_md_freshness.py` gates it against `src/notebooklm/`.
- Individual RPC payloads and every generated protobuf type would churn faster than they teach;
  use [`rpc-reference.md`](../rpc-reference.md), [`rpc-development.md`](../rpc-development.md), and
  the [Android evidence index](../android/README.md) instead.
- Command-by-command and method-by-method inventories remain in the
  [CLI reference](../cli-reference.md) and [Python API](../python-api.md); the diagrams explain
  ownership and flow rather than duplicate reference documentation.

## Updating a diagram

The repository commits Archify JSON sources and self-contained HTML viewers, but it does not
currently vendor or pin the generator. Set `ARCHIFY_ROOT` to the installed Archify skill/package,
record its version in the PR, and review the generated diff. Use the diagram's type
(`architecture`, `workflow`, `sequence`, `dataflow`, or `lifecycle`):

```bash
ARCHIFY_ROOT=/path/to/archify
node "$ARCHIFY_ROOT/bin/archify.mjs" doctor --json
node "$ARCHIFY_ROOT/bin/archify.mjs" validate <type> <diagram.json> \
  --quality showcase --repo-root "$PWD" --json
node "$ARCHIFY_ROOT/bin/archify.mjs" deliver <type> <diagram.json> <diagram.html> \
  --quality showcase --repo-root "$PWD" --json
node "$ARCHIFY_ROOT/bin/archify.mjs" visual-check <diagram.html> --json
```

Inspect the light and dark screenshots from `visual-check`, then delete all generated
`*.visual-check.*` files. They are review evidence, not repository artifacts. Commit only the
JSON source and delivered HTML.
