# Comprehensive plan (rev 2) — CLI business-logic separation (`_app/` relocation)

**Status:** rev 2 — addresses both momus REJECTs (Claude + Codex) · 2026-06-08
**Branch:** `refactor/cli-business-logic`

Rev-1 was REJECTED by both reviewers (architecture endorsed; plan over-claimed). This rev fixes:
serializer inventory, the typed-Result-vs-dict contradiction, wrapper/patch-seam semantics, the
error-taxonomy split, a per-command resolver/RPC-order table, in-scope MCP dedup, and a tougher
prototype. Each fix is marked `[fixes: …]`.

## 1. Decision (unchanged — both oracles + both momus endorse it)

Relocate transport-neutral **business logic** into `src/notebooklm/_app/` (underscore-private,
ADR-0012). CLI / MCP / future HTTP are thin sibling adapters. `cli/services/*` shrink to adapters;
no `usecases/` ceremony, no DI container. Supersedes ADR-0021 on *placement*.

```
client.* (public domain API) → _app/ (neutral) → cli/(Click) + mcp/(FastMCP) [+ http/]
```

## 2. The reusable-unit contract — typed Result is canonical [fixes: download dict-vs-dataclass]

**Existence proof (lead with these, NOT download):** `source_clean.run_source_clean → SourceCleanResult`
and `source_research → SourceAddResearchResult` **already** return typed dataclasses consumed by
command-layer dispatchers that build the `--json` envelope. *That is the target end-state, already
shipped in this repo.*

```python
# _app/<domain>.py — neutral
@dataclass(frozen=True) class <Verb>Request: ...     # typed inputs; None=default; no json_output/Console/raw_args
@dataclass(frozen=True) class <Verb>Result: ...      # typed dataclass — NOT an envelope dict
def build_<verb>_plan(req) -> <Verb>Plan: ...        # sync, pure, raises typed ValidationError
async def execute_<verb>(plan, client, *, progress: ProgressSink|None=None) -> <Verb>Result: ...
```

The CLI adapter builds the `--json` envelope **from** the typed Result. **`download` is the
counter-example**, not the template: `execute_download` returns a **dict** today (download.py:502–516,
609). So **W1 is an explicit conversion** (dict → `DownloadResult` dataclass + CLI re-derives the exact
envelope keys), **not "near-verbatim."** That framing is deleted.

## 3. JSON serializer inventory [fixes: Critical #1 — name every envelope builder before moving]

Every domain's `--json` payload is built by exactly one of these; the refactor moves the *logic*
behind it but the **adapter** (CLI) keeps building the envelope with byte-identical keys:

| Domain | Envelope builder(s) today | After: typed Result → CLI envelope |
|---|---|---|
| download | `execute_download` returns dict (download.py:502–516); `_display_download_result` | `DownloadResult` → `download_cmd` builds envelope |
| generate | `_render_generation_result` over the already-typed `GenerationExecutionResult` | conforms; CLI keeps envelope build |
| list (all) | `listing.py::prepare_list` (listing.py:136) | `ListResult` → CLI `json_output_response` |
| source clean | `_dispatch_source_clean_result` (source_cmd.py:910) over `SourceCleanResult` | conforms; logic→_app, dispatcher stays in cmd |
| source research | `_render_add_research_result` (\_source_render.py:533) over `SourceAddResearchResult` | conforms |
| source fulltext | `_render_source_fulltext_result` (\_source_render.py:195) | split file-I/O→_app, envelope stays in cmd |
| errors (all) | `error_handler._output_error` / `build_error_envelope` (error_handler.py:111) | unchanged — stays in CLI |
| artifact/note/label | per-command mutation payloads | typed Result → CLI envelope |

**Rule:** a domain is only "done" once its envelope is rebuilt in the CLI adapter from a typed Result
with a **golden `--json` characterization test** proving byte-identical output (see §8).

## 4. Wrapper / patch-seam semantics [fixes: Critical #2 — the conftest.py:483 trap]

Verified: ~656 `patch("notebooklm.cli…")`, 443 of them `patch("…<x>_cmd.NotebookLMClient")` on the
**command modules**; only ~30 target `cli.services.*`; **101 target `cli.helpers.*`**
(`get_context_path`×34, `load_auth_from_storage`×29, `get_auth_tokens`×19, `console`×12). Re-export
wrappers alone do NOT preserve seams — if `_app.execute_*` *closes over* a moved symbol, a
`patch("cli.services.X")` no-ops (the repo already documents this at `tests/unit/cli/conftest.py:483`).

**Rules:**
1. **Do NOT move command modules.** Logic moves to `_app/`; the command **imports `_app` symbols into
   its own module namespace** and calls them by local name → `patch("cli.<x>_cmd.NotebookLMClient")`
   and `patch("cli.<x>_cmd.execute_*")` keep working (443 seams untouched).
2. **`cli/helpers.py` is a serialization point.** Its re-exports (`resolve.validate_id`,
   `get_context_path`, `load_auth_from_storage`, `get_auth_tokens`, `console`) **stay resolvable and
   patchable from `cli.helpers`** — the 101 patches must keep hitting live targets.
3. **`cli/resolve.py::validate_id` stays a wrapper** that calls `_app.resolve.validate_id` and
   re-raises `ValidationError` as `click.ClickException` — so `cli.resolve.validate_id` /
   `cli.helpers.validate_id` remain patchable (29 ClickException assertions) while `_app` has the pure core.
4. **`_app` internals call `_app` symbols** (never a Click wrapper). Anything a test must stub is
   either injected via the Protocol facade or looked up at call time — **never closed over at import**.
5. **Per moved function, the plan states the patch target location** (command-local vs helpers vs
   service-wrapper) in its wave ticket.

## 5. Error taxonomy — split, not unified [fixes: Critical #3]

`_app/errors.py` provides **classification only**: `classify(exc) -> (ErrorCategory, retriable: bool)`
(the exception→category function duplicated today in `error_handler`'s except-ladder + MCP's
`_CODE_TABLE`). **Each adapter keeps its OWN code vocabulary:**
- CLI `error_handler` maps `category → (CLI_code_string, exit_code, extra_builder)` preserving exact
  strings (`VALIDATION_ERROR`/`AUTH_ERROR`/`NOTEBOOKLM_ERROR`, exit 1/2/130) — `--json` byte-stable.
- MCP `_errors` maps `category → (MCP_code, retriable)` over its full pinned set
  (`RATE_LIMITED`/`AUTH`/`NOT_FOUND`/`VALIDATION`/`TIMEOUT`/`SERVER`/`NETWORK`/`RPC`/`ERROR`) — manifest-pinned.
A coverage test asserts every `NotebookLMError` subclass classifies in `_app` AND maps in *both* adapters.
**No single code table.** **`classify` is class-sensitive** where the adapters are: `ArtifactTimeoutError`
must classify distinctly from a generic `WaitTimeoutError` so the CLI keeps emitting `ARTIFACT_TIMEOUT`
(and `NotebookLimitError`→`NOTEBOOK_LIMIT`, `ConfigurationError`→`CONFIG_ERROR` keep their CLI codes).
The category enum is granular enough to recover every existing CLI code 1:1.

## 6. Resolver / RPC-order preservation [fixes: Critical #4 — per command]

Cassettes match on `rpcids` + decoded body shape (`vcr_config.py:290/474/694`), blind to code path,
**iff** the RPC call set/order/body-shape + full-ID fast paths are preserved.

| Prototype command | ID resolution today | Risk |
|---|---|---|
| `download *` | inside `execute_download` (download.py:627), full-ID fast-path kept (resolve.py:285) | **LOW** (verified) — no new preflight |
| `generate *` | inside `execute_generation` (generate.py:180) | **LOW** (verified) |
| `source clean` | notebook resolve in command; no per-source id | **LOW** (no new RPC) |
| `source add-research` | notebook resolve in command; full-ID fast path | **MED** — keep resolve order; assert no extra GET |

Rule: moving resolution between layers must NOT add a list/get preflight on a full-ID path. Each wave
runs **all 15** `tests/integration/cli_vcr/` suites.

## 7. Phases (Wave 0 → parallel domains → auth last → integration) — as rev 1 §4, with §4–6 rules applied.

## 8. Prototype scope (this session) [fixes: Important #5/#6 — include sources + golden tests + MCP dedup]

1. **Wave 0 (serialized):** `_app/{errors(classify),serialize(to_jsonable),resolve,events}` + boundary
   lint `test_app_boundary.py`. De-Click `validate_id` with the §4 wrapper discipline.
2. **Golden `--json` characterization tests FIRST** for the prototype commands (capture current
   `--json` output under the existing cassettes; assert unchanged after each move).
3. **W1 download** (the dict→dataclass conversion) — includes `download.py` + `download_helpers.py` +
   `_download_specs.py` (which imports `DownloadTypeSpec` from the service — dependency direction).
4. **W-sources: `source add-research`** (the hard case — `SourceAddResearchResult` typed Result +
   8-outcome command-layer envelope dispatcher + exit codes). Proves the real boundary.
5. **MCP dedup in-scope:** replace `mcp/_serialize.py` with `from ..._app.serialize import to_jsonable`
   on a slice of `feat/mcp-server` + a **byte-equivalence test** (`_app.to_jsonable` ≡ old MCP output
   on sample types). The cleanest dedup; proves the payoff. (`_ids`/`_errors` dedup deferred — they need
   the classification split landed first.)

## 9. Acceptance criteria [fixes: verifiability]

- All **16** `tests/integration/cli_vcr/` suites green **without re-recording**; golden `--json`
  characterization tests green; full default suite green; mypy + ruff clean.
- `test_app_boundary` green; `_app/` imports no `click`/`rich`/`cli.*`/`fastmcp`.
- `patch("cli.*")` seam audit: zero now-dead targets after each wave.
- **MCP dedup proven in-scope:** `mcp/_serialize.py` deleted, MCP uses `_app.serialize`, byte-equiv test green.
- No additions to `scripts/api-compat-allowlist.json` (underscore-private relocation).
- Major CLI options preserved (removals explicitly called out).

## 10. Execution (workflow shape)
- **Workflow 1 (serialized):** Wave 0 + golden tests (one agent, isolated worktree, polished) → full
  suite + boundary lint green → merge to `refactor/cli-business-logic`.
- **Workflow 2 (parallel):** W1 download + W-sources(add-research) — one agent per domain, own worktree,
  polished, runs its `cli_vcr`+golden+unit suites, merges back. Then the MCP-dedup slice.
- ADR-0021 rewritten as the relocation decision in the integration step.
