# Auth test-patch PR 5 evidence

Base: `6aabddad7dac2386608b5e0e0e0f8eb66f454c5c` (`origin/main` at branch creation)

This report preserves the base/head scenario evidence for the persistence and long-tail
migration. PR 1 is not on this branch, so these rows must be incorporated into its authored
scenario and survivor policies after the five branches are stacked; this report is not a
substitute for those validators.

## Standalone patch scorecard

| Projection | Base | Head | Delta |
| --- | ---: | ---: | ---: |
| `_auth` total sites | 243 | 199 | -44 |
| `_auth` private-name sites | 144 | 117 | -27 |
| `_auth` non-underscore sites | 99 | 82 | -17 |
| `_auth` distinct `module.attribute` targets | 71 | 67 | -4 |
| `_auth` files | 44 | 43 | -1 |
| `_auth` direct assignments | 6 | 6 | 0 |

The exact decreased v1 rows are:

| Module | Attribute | Idiom | Base | Head |
| --- | --- | --- | ---: | ---: |
| `account` | `enumerate_accounts` | `patch.object` | 8 | 6 |
| `account` | `extract_email_from_html` | `patch.object` | 3 | 1 |
| `cookies` | `build_httpx_cookies_from_storage` | `patch.object` | 4 | 2 |
| `master_token_file` | `_commit_master_token_json` | `monkeypatch.setattr` | 5 | 1 |
| `master_token_file` | `_ensure_secure_parent_dir` | `monkeypatch.setattr` | 3 | 0 |
| `master_token_file` | `_master_token_from_legacy_record` | `monkeypatch.setattr` | 3 | 0 |
| `master_token_file` | `_master_token_to_legacy_record` | `monkeypatch.setattr` | 2 | 1 |
| `master_token_file` | `_storage_state_lock_path` | `monkeypatch.setattr` | 2 | 0 |
| `profile_store` | `_commit_profile_json` | `monkeypatch.setattr` | 18 | 7 |
| `profile_store` | `filter_storage_state_cookies_by_domain_policy` | `monkeypatch.setattr` | 7 | 0 |
| `storage` | `ProfileStore` | `monkeypatch.setattr` | 6 | 3 |
| `storage` | `replace_profile_from_login` | `monkeypatch.setattr` | 2 | 1 |
| `tokens` | `_load_stored_auth` | `monkeypatch.setattr` | 6 | 3 |

No site moved to the public facade, browser package, class, singleton, fixture, or helper. The
remaining commit replacements are sealed-fault tests. Consolidated adapter sites stay lexically
inside parametrized test bodies, and their complete base-to-head mappings are below.

## Scenario mappings

| Base node or family | Head node or family | Preserved oracle | Affected SUT |
| --- | --- | --- | --- |
| `test_auth_master_token_file.py::test_private_read_probes_reads_parses_and_decodes_once_in_order` | same | real bytes still prove parsing and typed decoding; path probes preserve exact existence/read/parse order | `src/notebooklm/_auth/master_token_file.py`, `master_token_types.py` |
| `test_auth_master_token_file.py::test_absent_private_and_typed_reads_do_nothing_else` | same | absent file returns `None` without reading or parsing | same |
| `test_auth_master_token_file.py::test_public_present_read_probes_constructs_reads_parses_and_decodes_once` | same | the public adapter uses the real owner and codec after one probe/read/parse sequence | `master_token.py`, `master_token_file.py` |
| `test_auth_master_token_file.py::test_write_order_request_payload_and_release_are_exact` | same | a real atomic write occurs under the exact raw-path lock; parent exists before acquisition, bytes are exact before release, and the lock always exits | `master_token_file.py`, `credential_io.py`, `_atomic_io.py` |
| `test_auth_master_token_file.py::test_write_fails_closed_for_both_nonheld_states[...]` | same | both non-held states raise the exact error, release, and create no credential file | `master_token_file.py`, `storage_lock.py` |
| `test_auth_master_token_file.py::test_non_token_rejection_precedes_parent_lock_and_commit` | same | invalid input leaves the parent absent, proving no persistence side effect | `master_token_file.py` |
| `test_auth_master_token_file.py::test_arbitrary_relative_path_is_never_canonicalized` | same | actual bytes land at the raw relative spelling and the exact lock path retains `..` | `master_token_file.py`, `paths.py` |
| `test_auth_profile_store.py::test_cookie_no_change_and_identical_decided_document_result_table` | same | the APPLIED branch performs a real atomic write with exact JSON and log result | `profile_store.py`, `cookie_merge.py`, `credential_io.py` |
| `test_auth_profile_store_login.py::test_unheld_lock_reports_without_entering_transaction_body[...]` | same | typed lock miss, exact request/release, and absent output remain | `profile_store.py`, `storage_lock.py` |
| `test_auth_profile_store_login.py::test_raw_filter_fidelity_required_gate_and_keep_namespace_are_one_held_body` | same | real persisted JSON preserves the filtered raw rows, required-cookie gate, namespace, and one held transaction | `profile_store.py`, `cookie_filter.py`, `credential_io.py` |
| `test_auth_profile_store_login.py::test_required_rejection_uses_late_policy_attributes_and_precedes_destination` | same | exact typed rejection and predecessor bytes prove no backup or commit | `profile_store.py`, `cookie_policy.py` |
| `test_auth_profile_store_login.py::test_set_projection_uses_a_second_shared_memo_copy_and_never_aliases_commit` | same | two real writes preserve request isolation, paired projection values, and one shared deepcopy memo | `profile_store.py`, `profile_account.py` |
| `test_auth_profile_store_login.py::test_held_set_projection_copy_failure_releases_without_backup_or_commit` | same | exact projection error releases the lock and leaves no file | `profile_store.py` |
| `test_auth_profile_store_login.py::test_directives_construct_exact_distinct_namespaces[...]` | same | every directive/namespace pair is read back from actual JSON | `profile_store.py`, `profile_account.py` |
| `test_auth_profile_store_minted.py::test_store_uses_one_exact_raw_bounded_lock_and_never_orders_io` | same | exact lock request plus real JSON proves owner/account/cookie persistence | `profile_store.py`, `credential_io.py` |
| `test_auth_profile_store_minted.py::test_store_lock_miss_raises_exactly_before_every_held_operation[...]` | same | exact exception, release, and absent output prove zero body side effect | `profile_store.py`, `storage_lock.py` |
| `test_auth_profile_store_minted.py::test_corrupt_existing_destination_is_unknown_and_refused_without_filter[...]` | same | exact refusal and predecessor-byte equality remain | `profile_store.py`, `profile_document.py` |
| `test_auth_profile_store_minted.py::test_non_owner_failures_escape_unchanged_in_exact_order[filter]` | same | an input-owned exploding domain reaches the real filter and preserves error identity/order without rebinding it | `profile_store.py`, `cookie_filter.py` |
| `test_auth_profile_store_minted.py::test_adapter_eagerly_iterates_raw_jar_once_and_snapshots_both_inputs` | `test_adapter_composition_contract[snapshot]` | one eager jar observation, deep snapshot isolation, exact request, and typed-store delegation | `storage.py`, `profile_store.py`, `cookie_types.py` |
| `test_auth_profile_store_minted.py::test_adapter_snapshot_failure_precedes_store_parent_lock_filter_read_commit_and_logs` | `test_adapter_composition_contract[copy-failure]` | exact error identity, no store construction, no parent, and no log | same |
| `test_auth_profile_store_minted.py::test_adapter_translates_only_private_refusal_outside_handler_without_context[...]` | `test_adapter_composition_contract[unknown-owner]`; `[mismatched-owner]` | both exact messages become context-free `MasterTokenError` | `storage.py`, `master_token.py` |
| `test_auth_profile_store_minted.py::test_adapter_preserves_non_owner_exception_identity` | `test_adapter_composition_contract[non-owner-error]` | non-owner error object escapes unchanged | `storage.py` |
| `test_auth_profile_store_remint.py::test_lock_miss_is_typed_and_runs_zero_body[...]` | same | exact result/request/release and absent output | `profile_store.py`, `storage_lock.py` |
| `test_auth_profile_store_remint.py::test_unicode_carry_failure_precedes_filter_and_releases_lock` | same | Unicode error and exact predecessor bytes survive; no warning is emitted | `profile_store.py`, `profile_document.py` |
| `test_auth_profile_store_remint.py::test_filter_projection_fidelity_optional_and_isolation` | same | actual JSON preserves exact selected rows, namespace, and source isolation | `profile_store.py`, `cookie_filter.py` |
| `test_auth_profile_store_remint.py::test_filter_failure_escapes_after_release` | same | a fresh `ProfileDocument` owner raises during projection and the lock releases | `profile_store.py`, `profile_document.py` |
| `test_playwright_login_render_contract.py::TestLoginProgressSuccess::test_single_account_metadata_is_written` | same | real CLI/app rendering consumes a typed app-repair success and preserves byte-exact output | `cli/services/playwright_login.py`, `_app/login_browser.py`, `_app/profile.py` |
| `test_playwright_login_render_contract.py::TestAuthRefreshRepair::*` | same four nodes | success, quiet, ambiguity, and error render paths consume direct app-owner projections with exact CLI output | same |
| `test_storage_writer.py::test_replace_from_login_is_one_typed_store_delegation_and_exhaustive_projection[...]` and `test_replace_from_login_compatibility_account_translation_is_exact[...]` | `test_replace_from_login_is_one_typed_delegation_and_exhaustive_projection[...]` | the cross-product preserves every typed-result projection and every keep/clear/set translation, including explicit and default arguments | `storage.py`, `profile_store.py`, `profile_account.py` |
| `test_client.py::TestFromStorage::test_from_storage_uses_auth_storage_path_for_explicit_path` | `test_from_storage_projects_loaded_auth_and_registers_file_baseline[explicit-path]` | explicit path is forwarded once and reused from loaded auth | `client.py`, `_auth/tokens.py` |
| `test_client.py::TestFromStorage::test_from_storage_uses_auth_storage_path_for_profile` | `test_from_storage_projects_loaded_auth_and_registers_file_baseline[profile]` | profile is forwarded once and the loaded storage path is not re-resolved | same |
| `test_client.py::TestFromStorage::test_from_storage_registers_exact_file_store_and_baseline` | `test_from_storage_projects_loaded_auth_and_registers_file_baseline[file-baseline]` | exact store identity and ready baseline are registered once | `client.py`, `_web/transport/cookie_persistence.py` |
| `test_client.py::TestFromStorage::test_from_storage_preserves_none_storage_path_for_auth_json` | `test_from_storage_projects_loaded_auth_and_registers_file_baseline[inline-auth]` | inline auth stays fileless and path resolution is not invoked | `client.py`, `_auth/tokens.py` |

## Verification so far

- Changed persistence, client, storage-adapter, and CLI-render selection: `276 passed`.
- Ruff and `git diff --check` on all changed Python files: passed.
- Standalone legacy audit: `_auth` 199 sites (117 private, 82 public).
- Assembled PR 2 + PR 3 + PR 5 checkpoint: 117 sites (68 private) before PR 3's
  reviewer-requested final refresh cleanup.

No cassette, dependency manifest, lockfile, public signature, credential format, or unchecked
credential-write capability changed.
