# CLI test → `_app/` migration map

**Date:** 2026-06-08
**Branch:** `refactor/cli-business-logic`
**Status:** analysis only — no code/test moved yet

## Why this exists

The CLI refactor extracted business logic into the transport-neutral `src/notebooklm/_app/`
layer (Click-free core: validation, resolution, plan-building, classification, status
projection, retry/wait, junk detection, content selection). The **tests did not move with the
logic**. Result:

- `tests/unit/cli/` — 64 files, ~39,400 LOC
- `tests/unit/app/` — 5 files, ~910 LOC

Of the **25 `_app` modules**, only ~4 have a dedicated app-level test (`resolve`, `serialize`,
`errors`, `artifacts.status_view`). The other ~21 are exercised **only** indirectly through
`CliRunner`/mock-client CLI tests. The risk is **not** redundant double-testing — it's that
`_app` is under-tested directly and the CLI suite over-carries neutral-logic coverage.

This document maps which CLI tests should move down, split, consolidate, or stay.

## Four verdicts

- **MOVE** — test already calls a pure function directly (no `CliRunner`), reaching `_app`
  logic through a thin `cli.*` re-export. Retarget the import → physically move to
  `tests/unit/app/`. *Free win.*
- **SPLIT** — assertion is about neutral logic but is reached through `CliRunner`/exit-codes.
  Retarget the assertion at the `_app` function (new app test); leave a thin shell behind.
- **DUPLICATE** — neutral behavior is *already* covered by an existing `app/` test against a
  parallel/neutral impl → consolidate/delete, don't move.
- **KEEP** — genuinely CLI: rendering, `--json` envelope shape, exit codes, prompts, help text,
  file side-effects, auth/login.

> Test counts are approximate (sampled by reading imports + a representative spread of test
> bodies per file). Treat them as magnitudes, not exact figures.

## The map

| CLI test file | ~tests | MOVE | SPLIT | KEEP | Target `_app` module |
|---|--:|--:|--:|--:|---|
| **test_download_multi_artifact.py** | 26 | **26** | – | – | `download` (select_artifact, filename) — *wholesale* |
| **test_source.py** | 115 | **16** | 5 | 94 | `source_clean` (TestSourceCleanClassify), `source_add` (SSRF) |
| **test_generate.py** | 80 | **15** | 25 | 40 | `generate_retry` (backoff/retry), `generate_plans`, `language` |
| **test_skill.py** | 45 | **6–9** | 4 | 30 | `skill` (version/classify_target/reporting) |
| **test_language.py** | 30 | **2–4** | 6 | 20 | `language` (LanguageConfigStore, catalog) |
| **test_doctor.py** | 17 | – | **11** | 6 | `doctor` — *no app test yet* |
| **test_note.py** | 38 | – | 6–8 | 30 | `notes` (not-found/race/no-op/new-id) |
| **test_generate_characterization.py** | 16 | – | 5 | 11 | `generate_plans` |
| **test_label_cmd.py** | 17 | – | 4–5 | 12 | `labels` (resolution/yes-gate/error) |
| **test_chat.py** | 40 | – | 4 | 36 | `chat` — *no app test yet* |
| **test_download.py** | 55 | – | 10 | 45 | `download` (plan validation/partial-id) |
| **test_research.py** | 24 | – | 3 | 21 | `research` (flag validation/classify) |
| **test_notebook.py** | 40 | – | 3–4 | 36 | `notebooks` (error classification) |
| **test_source_content_rendering.py** | 16 | – | 3 | 13 | `source_content` (projection) |
| **test_share.py** | 16 | – | 2 | 14 | `sharing` (str→enum parse) |
| **test_research_characterization.py** | 13 | – | 2 | 11 | `research` (outcome classification) |
| **test_download_characterization.py** | 11 | – | 2 | 9 | `download` (format-extension) |
| **test_source_characterization.py** | 21 | – | 2 | 19 | `source_research` (flag conflict) |
| **test_source_cmd_coverage.py** | 16 | – | 1 | 15 | `source_add` (validate_url) |
| **test_resolve.py** | 52 | — | — | 12 | **DUPLICATE ~40** → `app/test_app_resolve.py` |
| test_source_refresh.py, test_resolver_characterization.py, test_artifact.py, test_agent.py, **+ ~30 auth/login/session/plumbing files** | — | 0 | 0 | all | KEEP — no neutral logic (or already covered in app/) |

## The free wins (clean MOVE — just change the import)

Already pure-function tests reaching `_app` through a `cli.*` re-export. They move to
`tests/unit/app/` with an import retarget, no rewrite:

1. **`test_download_multi_artifact.py` (~26) — moves wholesale.** `select_artifact` /
   `artifact_title_to_filename` are *defined* in `_app/download.py`, only re-exported via
   `cli.download_helpers`. Single highest-volume, zero-coupling win.
2. **`test_source.py::TestSourceCleanClassify` (~16).** Already calls `_classify_junk_sources([...])`
   directly → retarget to `_app.source_clean.classify_junk_sources`.
   - Tests: `test_error_status_is_flagged`, `test_gateway_titles_are_flagged`,
     `test_url_title_on_ready_source_is_not_deleted`, `test_dedup_keeps_oldest_and_flags_later_copies`,
     `test_dedup_strips_fragment`, `test_dedup_is_case_insensitive_on_scheme_and_host`,
     `test_undated_sources_go_to_end_of_sort`, `test_source_with_no_url_is_not_deduped`, …
3. **`test_generate.py::TestCalculateBackoffDelay` + `TestGenerateWithRetry` (~11–15).** Pure async
   retry/backoff imported from the `cli.services.artifact_generation` re-export →
   `_app/generate_retry.py`.
   - Tests: `test_retry_on_rate_limit`, `test_retry_exhausted_reraises`,
     `test_retry_delays_increase_exponentially`, `test_retry_delay_caps_at_max`, + all of
     `TestCalculateBackoffDelay`.
4. **`test_skill.py`: `TestSkillVersionExtraction`, `TestAddVersionComment`,
   `TestSkillSourceFallback`, `TestSkillInstallReporting` (~6–9).** Already call module/service
   functions directly → `_app/skill.py` (`classify_target`, version/path helpers,
   `report_mixed_no_clobber_up_to_date`).
5. **`test_language.py::TestGetConfigErrorPaths` (~2).** `test_get_config_json_decode_error`,
   `test_get_config_oserror` call `get_config()` directly → `_app/language.LanguageConfigStore`.

**≈ 65–70 tests** are free MOVEs.

## Two `_app` modules have *no* app test at all

`_app/chat.py` and `_app/doctor.py` are exercised **only** through CLI tests today. The SPLIT work
for them isn't a move — it's **net-new coverage**:

- **`test_doctor.py` → new `app/test_app_doctor.py`** (~11): the four checks + fixes produce the
  exact `{"status","detail"}` strings; retarget at `run_checks(*, fix, paths) -> DoctorReport`,
  asserting `report.checks[...]`. CliRunner stays only for `--json` shape + exit-code mapping.
  - Candidates: `test_doctor_reports_clean_profile_layout`,
    `..._legacy_layout_without_startup_migration`, `..._missing_profile_dir`,
    `..._invalid_storage_json`, `..._invalid_storage_root_shape`, `..._invalid_storage_cookie_shape`,
    `..._cookies_missing_sid`, `..._warns_when_config_default_profile_is_missing`,
    `..._invalid_config_root_shape`, `test_doctor_fix_creates_missing_profile_dir`,
    `test_doctor_fix_migrates_legacy_layout`.
- **`test_chat.py` → new `app/test_app_chat.py`** (~4): conversation-id selection ladder
  (`determine_conversation_id`), `execute_configure`/`ConfigureResult` projection,
  `validate_ask_flags`.
  - Candidates: `TestAskServerResumed::test_ask_shows_resumed_when_no_local_conv_but_server_has_one`,
    `::test_ask_shows_turn_number_for_local_follow_up`,
    `TestConfigureJsonOutput::test_configure_persona_json`,
    `test_ask_new_conflicts_with_conversation_id`.

## The resolve duplication (separate from moves)

`test_resolve.py` is ~52 tests, **~40 of which duplicate `app/test_app_resolve.py`** — but against
`cli/resolve.py`'s *own parallel* `validate_id` / `resolve_partial_id_in_items` (it does **not**
delegate to `_app/resolve.py`). Same semantics (case-insensitive match, unique-prefix expansion,
ambiguous→"up to 5 candidates", UUID fast-path-skips-listing, blank-id rejection) asserted twice
against two implementations.

Real fix: **collapse the parallel impl into `_app/resolve.py`**, then delete the ~40 duplicate CLI
tests. The remaining ~12 stay — env-var → context-file → `SystemExit` ladder
(`TestRequireNotebookEnvVarFallback`), stderr-routing in `--json` mode, the
`download_helpers`-wording contract (`TestEntitySpecificPartialArtifactId`). This is the one place
with genuine redundant double-testing.

## The KEEP bucket (no action)

- `test_artifact.py` (~58) — neutral half already covered by `app/test_app_artifacts.py`
  (`status_view`/`ArtifactStatusView`); remainder is rendering, `--json` envelope, exit codes,
  prompts, help.
- `test_resolver_characterization.py` (~11) — CliRunner exit-code/IO/side-effect characterization.
- `test_source_refresh.py` (~7) — `--exit-on-stale` exit-code policy + help text.
- `test_agent.py` (~10) — `agent show` has **no** `_app` module (pure CLI).
- **~30 auth/login/session/plumbing files** — `test_login*`, `test_cookie*`,
  `test_playwright_login*`, `test_firefox_accounts`, `test_rookiepy_errors`, `test_session*`,
  `test_use*`, `test_status_clear`, `test_profile`, `test_auth_subcommands`, `test_completion`,
  `test_grouped`, `test_root_group`, `test_help_text`, `test_encoding`, `test_helpers*`,
  `test_options_validation`, `test_error_handler`, `test_quiet*`, `test_prompt_file`,
  `test_context_coverage`, `test_cli_contract`, `test_json_validation_contract`,
  `test_services_boundary`, `test_storage_context_isolation`, `test_cli_session_local`. Auth/login
  + Click plumbing + contract/boundary guards — no neutral layer to host them.

## Template to imitate

`tests/unit/cli/test_label_listing.py` is **already migrated** and is the destination shape: it
calls `resolve_label_id(client, ...)` and `execute_label_list(client, plan)` directly with a
`MagicMock` client — no Click, no `CliRunner` — and re-exports from `_app/labels.py`. New app-level
tests should look like this. (Its `test_module_is_boundary_clean` ADR-0008 guard stays in cli/.)

## Recommended sequencing

1. **Free MOVEs first** (~65 tests, mechanical) — biggest coverage shift for least risk;
   `test_download_multi_artifact.py` is the flagship.
2. **New app tests for the untested modules** — `doctor` then `chat` (closes the worst
   direct-coverage gaps).
3. **Resolve consolidation** — fold `cli/resolve.py` into `_app/resolve.py`, delete the ~40
   duplicates.
4. **SPLITs as you touch each command** — opportunistic; each leaves a thinner CLI shell.
