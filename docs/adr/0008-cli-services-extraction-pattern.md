# ADR-0008: `cli/services/` extraction pattern

## Status

Accepted (retroactive). Documents the pattern established during the cli-ux-remediation arc (8 phases / 27 PRs, completed 2026-05-14/15) and reinforced by the tier-10 foundation-decomposition work.

## Context

CLI commands grew organically: every new feature added a new Click command (or sub-command), and the command body co-located argument parsing, validation, business logic, error handling, and presentation. By the time of the cli-ux audit, the largest CLI module (`cli/session.py`, later renamed `cli/session_cmd.py`, the `login` / `use` / `status` / `clear` family) had reached ~1,973 lines with 64 top-level / async functions. Most of those functions were *business logic*: browser-profile enumeration, cookie extraction, multi-account fan-out, profile validation.

The audit identified three concrete failure modes:

1. **Click commands were not unit-testable in isolation.** Testing "what happens if browser profile enumeration returns three entries" required either driving the full Click test runner (slow, mixes parsing + business logic + presentation in every assertion) or `monkeypatch.setattr("notebooklm.cli.session_cmd._enumerate_…", fake)` — the test-monkeypatch gravity pattern that ADR-0003 and ADR-0007 describe.
2. **Business logic was uncoupled from re-use.** The same browser-profile enumeration logic would have been useful from the Python API, but importing it required reaching past Click decorators into `cli/session.py`. The CLI module became a sink for logic that should have been library-level.
3. **CLI commands could not shrink.** Even the trivial commands carried ~50 lines of validation / setup before they could call into the actual work, because the "actual work" was inline.

The cli-ux-remediation arc moved business logic into a sibling sub-package:

```text
src/notebooklm/cli/services/
├── __init__.py
├── download.py              download workflow planning
├── generate.py              generation workflow planning
├── login/                   browser-cookie auth flows + profile enumeration
├── research.py              research task workflow helpers
├── source_listing.py        source list rendering data
├── source_mutations.py      source mutation helpers
└── source_research.py       source-grounded research helpers
```

Each `cli/services/<name>.py` module exposes CLI-adjacent workflow logic for one domain. The Click commands in `cli/<name>_cmd.py` shrink to thin shells: parse arguments, construct a service-layer plan or call, render results. Shared user-facing workflows that are no longer CLI-specific now live in `src/notebooklm/_app/` so both CLI, MCP, and server adapters can reuse them without importing Click.

A typical service module exposes:

- A `Plan` dataclass that names every decision the command will make (so the plan can be validated / displayed without execution).
- A `build_<plan>(args) -> Plan` pure function that validates inputs and constructs the plan.
- An `execute_<plan>(plan, facade) -> Result` async function that performs the work, calling out through a `Protocol`-typed facade (so tests can substitute a fake without touching Click).

The Click command then becomes `parse args → build_plan → execute_plan(plan, real_facade) → render`. Each step is independently testable.

## Decision

CLI business logic lives in `src/notebooklm/cli/services/<domain>.py` when it is genuinely CLI-specific, and in `src/notebooklm/_app/<domain>.py` when it is adapter-neutral. Click commands in `src/notebooklm/cli/<domain>_cmd.py` are thin shells that:

1. Parse arguments via Click decorators.
2. Validate inputs (using helpers from the service module where possible).
3. Construct a service-layer plan or call the service function directly.
4. Render results to the console via the rendering helpers in `cli/rendering.py`.

Service modules must:

- Be importable from non-CLI contexts (no top-level Click imports; no `click.echo` calls; no reliance on a Click context).
- Define their external collaborators as `Protocol` types (e.g. `SourceAddFacade` in `cli/services/source_add.py`) so tests can pass fakes.
- Keep presentation concerns out of the module — return structured results, let the caller decide how to render.
- Live next to their consumers — one service module per domain, *not* a `cli/services/utils.py` grab-bag.

During staged migrations, transitional service modules may appear in the
`tests/unit/cli/test_services_boundary.py` inventory with exact documented
violations. That inventory is currently empty; every module under
`cli/services/` must be classified as guarded or explicitly waived, and new
rendering or Click reach-ins fail the boundary tests.

The pattern is deliberately *light*: there is no service-layer base class, no DI container, no plugin system. Service modules are plain Python modules with plain functions.

## Consequences

**Wanted:**

- CLI commands shrink toward thin shells. The active command modules are named `*_cmd.py`; `cli/session_cmd.py` is below the historical ≤1,100-line target after the retired patch-surface bridge was removed, and module-size ratchets guard the remaining large CLI modules.
- Business logic is unit-testable without driving Click. Tests can call `build_source_add_plan(...)` directly and assert on the returned plan; tests can call `execute_source_add(plan, fake_facade)` and assert on the facade calls.
- The service modules document the *contract* of each CLI domain via their `Plan` dataclass and their facade `Protocol`. A reviewer reading `cli/services/source_add.py` sees the entire decision graph for `source add` in one file.
- Business logic is re-usable. Adapter-neutral workflows should live under `_app/` (for example `_app/source_clean.py`), while `cli/services/` keeps CLI-only planning and compatibility helpers.
- The pattern composes with ADR-0007's test-fixture pattern (constructor injection): service-layer functions take their collaborators as parameters, so test fixtures provide them; no monkeypatching of module globals.

**Unwanted:**

- Some Click commands have *no* business logic and still gain a service module if they cross a complexity threshold; reviewers must agree on where that threshold is. The current rule of thumb is "if the command body exceeds 50 lines or has more than one branch on validation results, extract a service."
- The pattern *requires* a `Protocol`-typed facade for testability; in some cases the protocol has a single implementer (`NotebookLMClient`) and looks ceremonial. The audit ADR-0002 calls out this exact failure mode for capability Protocols; the mitigation here is that the facade Protocols are narrow (per-service, listing only the methods that service uses) so they do not slip into fat-union shape.
- The split is visible in the file count. The login service is now a package of small modules rather than a single `login.py`, and some workflows also have an `_app/` module. The trade is "several reviewable files" vs "one unreviewable file"; the audit chose the former.

## Alternatives considered

- **Keep business logic inline in `cli/<command>_cmd.py`.** Rejected. The retired proxy block in `cli/session_cmd.py` demonstrated the anti-pattern: the file became a sink of helper functions, monkeypatch surfaces, and "import this for tests" hooks. The lesson is that business logic in CLI modules *will* attract test gravity, and the durable fix is to move the logic out.
- **Full `cli/<verb>/<noun>.py` hierarchy** (e.g. `cli/generate/audio.py`, `cli/generate/video.py`). Rejected as overkill for the current size of the surface. The flat `cli/<noun>.py` + `cli/services/<noun>.py` pattern handles the current 9-command surface cleanly; a hierarchical layout would add directory noise without solving a real problem. The pattern is open to revisit if the command count doubles.
- **A service-locator / DI container (e.g. `wired`, `dependency-injector`).** Rejected. The codebase has < 10 service modules and < 30 distinct collaborators; the cognitive cost of a DI framework dwarfs the benefit. Plain Python imports + `Protocol`-typed parameters are sufficient at this scale.
- **Move business logic into the Python API layer (`_<domain>.py`) and have the CLI call the Python API.** Partial alternative, applied where it fits. The Python API (`NotebooksAPI`, `SourcesAPI`, etc.) carries the *protocol-level* concerns (one RPC = one method). CLI services carry the *workflow-level* concerns (validate user input, choose between two RPC paths, fan out across multiple accounts). The split between "protocol verb" (Python API) and "workflow verb" (CLI service) is real and intentional; not every CLI helper belongs on the Python API.
- **Co-locate the service module inside the same file as the command (e.g. `cli/source.py::services`).** Rejected. Single-file co-location was the starting point; the cli-ux audit measured the test friction (every test had to import from a file that also imported Click) and chose physical separation. The cost of one extra import is well below the cost of mixing Click decorators with importable business logic.
