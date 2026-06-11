# ADR-0021: Transport-neutral application layer (`_app/`)

## Status

Accepted — realized on the `refactor/cli-business-logic` prototype branch and adopted on main. Supersedes, on the question of *placement*, the earlier "wire transport-neutral utilities **in place**" proposal: that proposal's in-place framing is withdrawn in favour of relocation into a dedicated package.

## Context

The CLI layer (`cli/`) carried business logic — id validation/resolution, plan-building, status projection, retry/wait orchestration, junk-source detection, content selection, `auth check`/`doctor` diagnostics — entangled with Click/Rich presentation. ADR-0008 (CLI-services extraction) moved much of it into `cli/services/`, but those services kept **signature-level** Click coupling (`json_output` flags, `Console`/`raw_args`/`emit_status` seams). Additional front-ends — the FastMCP server and the FastAPI REST server — therefore needed the logic without importing Click.

Two independent oracle reviews (Claude + Codex) converged on relocating the neutral core out of `cli/` rather than wiring utilities in place; momus (Claude + Codex) cleared the resulting plan after a rev pass. This ADR records the decision.

## Decision

Relocate transport-neutral **business logic** into `src/notebooklm/_app/` (underscore-private per ADR-0012). CLI / MCP / REST are thin sibling **adapters** over it.

```
client.* (public domain API) → _app/ (neutral) → cli/ (Click) + mcp/ (FastMCP) + server/ (FastAPI)
```

- **Contract.** Per verb: typed frozen-dataclass `Request`/`Plan`/`Result`; a pure `build_<verb>_plan(req) -> Plan` (raises the public `notebooklm.exceptions` hierarchy on bad input); `async execute_<verb>(plan, client, *, progress: ProgressSink | None) -> Result`. The adapter parses its own inputs into the request, calls the neutral core, and renders the typed result into its own envelope vocabulary — the CLI builds the byte-stable `--json` envelope per ADR-0015.
- **Boundary.** `_app/` imports no transport framework (`click`, `rich`, `fastmcp`, `fastapi`, `starlette`, `uvicorn`), no adapter sibling (`notebooklm.cli`, `notebooklm.server`), no RPC runtime sibling (`notebooklm.rpc`), and no private `notebooklm._*` runtime sibling outside `_app` itself (enforced by `tests/_guardrails/test_app_boundary.py`). Rich/Click/FastMCP/FastAPI-coupled collaborators (consoles, prompters, spinners, importers, resolvers, request/response objects) are **injected** as callables/Protocols.
- **Errors.** `_app` raises the public `notebooklm.exceptions` hierarchy for library/domain failures; local deterministic adapter-input exceptions are either public-base subclasses or caught and translated before crossing the adapter boundary. Classification is centralized in `_app.errors.classify(exc) -> ClassifiedError` — the single neutral source of the failure **category** decision (per ADR-0019). Each adapter keeps its **own** code vocabulary and projects the category onto it: the CLI `error_handler` onto string codes + exit codes, the MCP server onto its manifest-pinned codes, and the REST server onto HTTP status + `{"error": {"category": "...", "message": "..."}}`. Consistency gates fail CI if adapter projections drift from `classify`. No adapter envelope-building lives in `_app` (no `.payload` / `to_envelope`); genuinely transport-neutral shaping shared by adapters is hoisted into `_app.serialize` (`to_jsonable`, `source_summary`).
- **Patch-seam discipline.** Command modules are **not** moved; anything a test stubs is read at call time or injected — never closed over at import (the trap documented at `tests/unit/cli/conftest.py`). *Amended #1481 (2026-06-08):* the per-command `patch("...<x>_cmd.NotebookLMClient")` client seams are **retired**. Command bodies resolve the client via `cli.auth_runtime.resolve_client_factory(ctx)`, injected through Click's `ctx.obj["client_factory"]`; tests substitute a fake with `runner.invoke(..., obj=inject_client(mock_client))`, and a recurrence gate (`tests/_guardrails/test_no_cli_client_patch_surface.py`) forbids re-exposing the name on any `*_cmd` module. The eight pure re-export `cli/services/*` shells that survived only to preserve import / call-time seams were collapsed into their `_app/` cores. Note `ctx.obj["client_factory"]` is the CLI **adapter's** client seam — a second front-end (MCP / HTTP) injects its client through the neutral `execute_<verb>(plan, client)` signature, not this Click-specific key.
- **Cassette invariance.** The relocation preserves the RPC call set/order/body-shape and the full-id fast paths, so the existing VCR cassettes (matched on `rpcids` + decoded body shape, blind to code path) stay valid **without re-recording**.

## What stays in the adapter (not relocated)

The "would a headless server call this?" test decides. Presentation/interactive code stays in `cli/`: Rich rendering, `--json` envelope assembly, exit-code policy, interactive login (playwright / browser-cookie / prompts). Two illustrative calls:

- `cli/resolve.py` stays a rich **adapter** over the pure `_app/resolve.py` **core** — it adds `ClickException` (not `ValidationError`), console `emit_status`, `entity_name`/`list_command` message hints, the `allow_full_id_passthrough` flag, and a pluggable `error_factory`. It is a justified adapter/core split, not duplication, so it was **not** collapsed.
- `agent show` stays pure presentation (no neutral core — nothing a headless caller would invoke).

## Consequences

- Additional front-ends reuse the neutral core directly. MCP and REST routes now import `_app.serialize.to_jsonable` / `source_summary` instead of maintaining private serializers.
- `_app` gains fast, failure-localizing **unit** tests alongside the CLI's **integration**/cassette tests — layered coverage, not redundancy (measured ≈89 % CLI-over-`_app` line overlap is the unit/integration split, not same-layer duplication).
- More indirection (adapter → `_app`), held in check by the boundary lint and the typed contract.
- Trade-off accepted: not every CLI test was pushed down; the CLI keeps its end-to-end coverage while `_app` adds direct coverage.

## Related

ADR-0008 (CLI-services extraction), ADR-0012 (implementation-surface convention), ADR-0015 (`--json` envelope contract), ADR-0019 (error-and-return contract).
