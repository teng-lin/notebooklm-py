# Auth test-patch PR 2 evidence

Base: `6aabddad7dac2386608b5e0e0e0f8eb66f454c5c` (`origin/main` at branch creation)

This report preserves the base/head scenario evidence needed to integrate this test-only
migration with the schema-v2 behavior and lifecycle policies introduced by PR 1. PR 1 is not on
this branch, so this report is the source for the later authored policy rows; it is not a
substitute for those validators after integration.

## Patch scorecard

| Projection | Base | Head | Delta |
| --- | ---: | ---: | ---: |
| `_auth` total sites | 243 | 213 | -30 |
| `_auth` private-name sites | 144 | 129 | -15 |
| `_auth` non-underscore sites | 99 | 84 | -15 |
| `_auth` distinct `module.attribute` targets | 71 | 68 | -3 |
| `_auth` files | 44 | 41 | -3 |
| `_auth` direct assignments | 6 | 0 | -6 |
| `_browser` total sites | 66 | 66 | 0 |
| Combined private-package sites | 309 | 279 | -30 |
| Combined private-name sites | 161 | 146 | -15 |
| Combined package-qualified targets | 85 | 82 | -3 |

The exact decreased/removed v1 rows are:

| Module | Attribute | Idiom | Base | Head |
| --- | --- | --- | ---: | ---: |
| `account` | `_probe_authuser` | `monkeypatch.setattr` | 2 | 0 |
| `cookie_policy` | `_SECONDARY_BINDING_WARNED` | assignment | 4 | 0 |
| `master_token` | `MasterTokenFile` | `monkeypatch.setattr` | 4 | 1 |
| `profile_store` | `_STORAGE_LOCKS` | assignment | 2 | 0 |
| `profile_store` | `_STORAGE_LOCKS` | `monkeypatch.setattr` | 6 | 2 |
| `profile_store` | `_commit_profile_json` | `monkeypatch.setattr` | 18 | 15 |
| `profile_store` | `filter_storage_state_cookies_by_domain_policy` | `monkeypatch.setattr` | 7 | 2 |
| `psidts_recovery` | `recover_psidts_in_memory` | `monkeypatch.setattr` | 7 | 1 |
| `tokens` | `resolve_auth_json_env` | `monkeypatch.setattr` | 1 | 0 |

No module patch was moved to a class, process-default object, public facade, or browser module.
The test diff removes four raw warning-flag assignments, two raw default-lock assignments, four
raw root-fixture container clears, and one mutation of a process-default lock manager. The only
new shared-owner calls are the named lifecycle operations described below. Tests that replace an
`acquire` method do so only on a fresh per-test `StorageLockManager` instance.

## Lifecycle cleanup migration

`tests/conftest.py::_reset_poke_state` changes for every collected test because it is autouse.
Its affected production paths and exact replacement operations are:

| Base mutation | Head operation | Affected SUT | Dedicated verification |
| --- | --- | --- | --- |
| clear `_LAST_POKE_ATTEMPT_MONOTONIC` and `_POKE_LOCKS_BY_LOOP` through facade aliases | `keepalive._reset_poke_state_for_tests()` before and after | `src/notebooklm/_auth/keepalive.py` | `tests/unit/test_auth_state_owners.py::test_rotation_state_loop_lock_and_safe_reset`; `tests/unit/test_auth_state_owners.py::test_rotation_state_boundaries_aliases_isolation_and_atomic_threads` |
| assign `cookie_policy._SECONDARY_BINDING_WARNED = False` before and after | lock-synchronized `cookie_policy._reset_secondary_binding_warning_for_tests()` | `src/notebooklm/_auth/cookie_policy.py` | `tests/unit/test_warning_dedupe.py::test_secondary_binding_warning_reset_rearms_after_synchronized_cleanup`; `tests/unit/test_warning_dedupe.py::test_secondary_binding_warns_exactly_once_under_asyncio_gather` |
| scheduler reset around tests | drain then existing `LegacyPromotionScheduler._reset_for_tests()` | `src/notebooklm/_auth/profile_migration.py` | `tests/unit/test_auth_profile_migration.py::test_reset_reopens_the_scheduler_for_the_next_test` |

The local fixture `tests/unit/test_warning_dedupe.py::_reset_warning_flags` now uses only the
cookie-warning reset operation. Its complete consumer set is the three test nodes in that module.
The PR-1 fixture-closure collector should expand the root autouse fixture to the complete base/head
collection when authoring the lifecycle policy.

## Modified scenarios and affected SUT

Every changed test node is listed below. A parameter-family prefix denotes the complete collected
family; the concrete head IDs were collected and are shown where parameters exist.

| Base/head node or exact mapping | Preserved oracle | Affected SUT paths |
| --- | --- | --- |
| `tests/unit/cli/test_session_edge_cases.py::TestLoginWindowsPermissions::test_windows_storage_chmod_skipped` | win32 permission branches still perform the write and call neither `chmod` nor `fchmod`; the store now receives a fresh held lock owner | `src/notebooklm/_auth/profile_store.py`, `src/notebooklm/_auth/storage.py`, `src/notebooklm/_atomic_io.py` |
| `tests/unit/test_auth_account_coverage.py::TestEnumerateAccountsPokeHook::test_poke_session_hook_invoked` | poke precedes real HTTP account probes and single-account fallback projection | `src/notebooklm/_auth/account.py` |
| `tests/unit/test_auth_account_coverage.py::TestEnumerateAccountsPokeHook::test_poke_session_none_skips_hook` | absent poke plus real HTTP probes preserves the no-hook branch | `src/notebooklm/_auth/account.py` |
| `tests/unit/test_auth_account_coverage.py::TestClearInBandLockFailure::test_lock_unavailable_is_swallowed` | best-effort clear leaves bytes untouched on unavailable lock, using a fresh `ProfileStore` | `src/notebooklm/_auth/profile_store.py`, `src/notebooklm/_auth/storage.py` |
| `tests/unit/test_auth_cookie_save_race.py::TestFlockUnavailableWarning::test_warning_emitted_when_lock_unavailable` | first unavailable blocking lock emits exactly one warning while merge behavior still executes | `src/notebooklm/_auth/profile_store.py`, `src/notebooklm/_auth/storage_lock.py`, `src/notebooklm/_auth/cookie_merge.py` |
| `tests/unit/test_auth_cookie_save_race.py::TestFlockUnavailableWarning::test_warning_emitted_only_once_per_process` | three operations on one fresh owner emit one warning | same as preceding row |
| `tests/unit/test_auth_master_token_file.py::test_public_probe_error_escapes_same_object_without_constructing` | the exact `Path.exists` exception, cause, and context still escape | `src/notebooklm/_auth/master_token.py` |
| `tests/unit/test_auth_master_token_file.py::test_public_constructor_error_after_present_probe_escapes_same_object` -> `tests/unit/test_auth_master_token_file.py::test_public_present_malformed_record_projects_master_token_error` | replaces an impossible constructor fault with real malformed stored bytes and the exact compatibility error projection | `src/notebooklm/_auth/master_token.py`, `src/notebooklm/_auth/master_token_file.py`, `src/notebooklm/_auth/master_token_types.py` |
| `tests/unit/test_auth_master_token_file.py::test_public_present_read_probes_constructs_reads_parses_and_decodes_once` | probe/read/parse/decode ordering and raw compatibility result; owner construction is real | `src/notebooklm/_auth/master_token.py`, `src/notebooklm/_auth/master_token_file.py` |
| `tests/unit/test_auth_profile_store_login.py::test_unheld_lock_reports_without_entering_transaction_body[LockState.CONTENDED]` | typed lock-miss result, exact request, release, no output file, and a fresh source sentinel proving zero body entry | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_profile_store_login.py::test_unheld_lock_reports_without_entering_transaction_body[LockState.UNAVAILABLE]` | same contract for infrastructure failure | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_profile_store_minted.py::test_store_lock_miss_raises_exactly_before_every_held_operation[LockState.CONTENDED]` | exact exception/cause/context, release, and a fresh cookie-domain sentinel proving zero body entry | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_profile_store_minted.py::test_store_lock_miss_raises_exactly_before_every_held_operation[LockState.UNAVAILABLE]` | same contract for infrastructure failure | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_profile_store_minted.py::test_corrupt_existing_destination_is_unknown_and_refused_without_filter[{]` | corrupt-object owner refusal leaves exact bytes untouched; a fresh cookie-domain sentinel proves filtering is not reached | `src/notebooklm/_auth/profile_store.py`, `src/notebooklm/_auth/profile_document.py` |
| `tests/unit/test_auth_profile_store_minted.py::test_corrupt_existing_destination_is_unknown_and_refused_without_filter[[]]` | non-object owner refusal leaves exact bytes untouched | same as preceding row |
| `tests/unit/test_auth_profile_store_remint.py::test_lock_miss_is_typed_and_runs_zero_body[LockState.CONTENDED]` | typed result, exact request, release, absent output, and a fresh source sentinel proving zero body entry | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_profile_store_remint.py::test_lock_miss_is_typed_and_runs_zero_body[LockState.UNAVAILABLE]` | same contract for infrastructure failure | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_profile_store_remint.py::test_unicode_carry_failure_precedes_filter_and_releases_lock` | Unicode error escapes after release, predecessor bytes remain exact, and a fresh source sentinel proves filtering was not reached | `src/notebooklm/_auth/profile_store.py` |
| `tests/unit/test_auth_stored_auth.py::test_inline_source_resolution_parses_one_captured_environment_value` | real environment resolution produces an `InlineAuthSource` with captured account data | `src/notebooklm/_auth/tokens.py`, `src/notebooklm/_auth/paths.py`, `src/notebooklm/_auth/profile_document.py` |
| `tests/unit/test_auth_validate_heal_split.py::TestValidateIsPure::test_validate_never_fires_a_heal` | direct pure validation returns failure without mutation or network-capable composition | `src/notebooklm/_auth/psidts_recovery.py`, `src/notebooklm/_auth/cookie_policy.py` |
| `tests/unit/test_auth_validate_heal_split.py::TestWrapperComposition::test_valid_set_short_circuits_before_healing` | valid input completes without a RotateCookies request | same as preceding row |
| `tests/unit/test_auth_validate_heal_split.py::TestWrapperComposition::test_successful_heal_clears_the_error_and_mutates_in_place` | real mocked HTTP boundary mints routed PSIDTS; result and caller rows both advance | `src/notebooklm/_auth/psidts_recovery.py`, `src/notebooklm/_auth/keepalive.py`, `src/notebooklm/_auth/mint_service.py` |
| `tests/unit/test_auth_validate_heal_split.py::TestWrapperComposition::test_declined_heal_preserves_the_original_error` | real unrotatable input preserves the original typed reason and exact rows | `src/notebooklm/_auth/psidts_recovery.py`, `src/notebooklm/_auth/cookie_policy.py` |
| `tests/unit/test_auth_validate_heal_split.py::TestWrapperComposition::test_post_heal_recheck_is_presence_only` | real mocked rotation plus one retained composition probe preserves the one pre-heal routing check | same as successful-heal row |
| `tests/unit/test_auth_validate_heal_split.py::TestHealIsSeparable::test_heal_delegates_to_the_single_rotation_implementation` | real mocked HTTP boundary proves heal result and in-place row mutation | same as successful-heal row |
| `tests/unit/test_warning_dedupe.py::test_secondary_binding_warns_exactly_once_under_asyncio_gather` | 100 calls still emit one warning and claim the flag | `src/notebooklm/_auth/cookie_policy.py` |
| new `tests/unit/test_warning_dedupe.py::test_secondary_binding_warning_reset_rearms_after_synchronized_cleanup` | synchronized reset re-arms the process warning exactly once | `src/notebooklm/_auth/cookie_policy.py` |
| `tests/unit/test_warning_dedupe.py::test_flock_unavailable_warns_exactly_once_under_asyncio_gather` | 100 operations on a fresh lock owner emit once; owner-local claim is asserted | `src/notebooklm/_auth/profile_store.py`, `src/notebooklm/_auth/storage_lock.py` |
| `tests/unit/test_audit_auth_patch_sites.py::test_real_function_local_import_sites_are_not_dropped` | live-location detector assertion now names only sites that deliberately remain | `scripts/audit_auth_patch_sites.py`, all changed patch-bearing tests |
| `tests/unit/test_audit_auth_patch_sites.py::test_live_replacement_patch_contract_and_scorecard_are_exact` | v1 live scorecard and target-row counts ratchet to the measured head | `scripts/audit_auth_patch_sites.py`, `tests/fixtures/baselines/auth_patch_sites.json` |
| `tests/unit/test_audit_auth_import_graph.py::test_live_projection_is_the_frozen_scorecard` | the import-graph line score records the narrow cookie-policy reset without changing any edge | `src/notebooklm/_auth/cookie_policy.py`, `scripts/audit_auth_import_graph.py`, `tests/fixtures/baselines/auth_import_graph.json` |

The unchanged direct-owner suites `tests/unit/test_auth_account_repair_service.py` and
`tests/unit/test_auth_master_token_bootstrap.py` remain the full account-repair and master-token
bootstrap behavior matrices. Their focused execution is part of the verification below; the few
remaining wrapper patches characterize call-time adapters rather than owner behavior.

## Verification

- Changed-scenario selection: `263 passed`.
- Plan focused auth/recovery/owner selection: `452 passed`.
- Audit, registry, and baseline-manifest selection: `401 passed`.
- Repository guardrail lane: `1981 passed, 1 skipped, 1 xfailed`.
- `ruff check` on all changed Python files: passed.
- `mypy src/notebooklm --ignore-missing-imports`: passed (`433` source files).
- `pre-commit` on every changed path: passed.
- V1 audit commands: `_auth` 213 sites; `_browser` 66 sites; zero direct assignments.

The repository-wide `ruff format --check .` reaches one unrelated pre-existing formatting defect
at `SKILL.md:223`; the formatter and format check pass for every Python file changed here.

No cassette, dependency manifest, package lockfile, public signature, credential schema, or
credential-write capability changed.
