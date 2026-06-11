#!/usr/bin/env python3
"""Strip internal audit-tracker references from comments, docstrings, and markdown.

Conservative bulk pass: only drops *parenthetical* ID references that ride
alongside prose and a small number of whole-sentence meta-commentary lines
that introduce the now-removed ID convention. Inline noun-form refs (e.g.
``T7.F2:`` at the start of a comment, ``audit §27 failure #1`` mid-sentence,
``Pre-T7.F4``) are intentionally left for targeted hand edits because the
surrounding sentence needs rewriting.

Whitespace is preserved outside the deleted-token regions; the script never
collapses runs of whitespace or reformats multi-line constructs.

The script is run repeatedly across the cleanup phases (Phase 1: src/, docs/,
CHANGELOG, pyproject, CLAUDE.md; Phase 2: tests/).
Each invocation is restricted to a phase-specific allow-list of file paths so
that one phase's run cannot accidentally edit another phase's files.

Usage::

    python scripts/_strip_audit_refs.py phase1
    python scripts/_strip_audit_refs.py phase2
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

T_ID = r"T\d+\.[A-Z][0-9a-z]*"
PR_T_ID = r"PR-" + T_ID
PHASE_T_ID = r"P\d+\.T\d+"
AUDIT_SEC = r"audit §\d+[a-z]?"
AUDIT_ROW = r"audit row [A-Z]?\d+[A-Za-z0-9]*"
AUDIT_SECTION = r"audit section \d+"
# CLI UX audit row IDs (``cli-ux-audit.md``): C1, C2, I1..I16, M1..M5.
# Match in parentheticals only; the bare letters are too common to strip
# unconditionally.
CLI_UX_ROW_ID = r"(?:[ICM]\d+(?:,\s*[ICM]\d+)*)"
# Phase markers (e.g. ``F8.T4``) used to coordinate landed-in-arc work.
PHASE_F_ID = r"F\d+\.T\d+"


# Parenthetical patterns — order matters: longest/most-specific first.
PAREN_PATTERNS: list[tuple[str, str]] = [
    # Combined inside one parens, with / or , separators
    (rf" \({T_ID}\s*[/,]\s*{AUDIT_SEC}\)", ""),
    (rf" \({AUDIT_SEC}\s*[/,]\s*{T_ID}\)", ""),
    (rf" \({T_ID}\s*[/,]\s*{AUDIT_SECTION}\)", ""),
    (rf" \({AUDIT_SECTION}\s*[/,]\s*{T_ID}\)", ""),
    # T + audit-section with extra tail text inside parens
    (rf" \({T_ID}\s+{AUDIT_SEC}(?:[^()]*)?\)", ""),
    (rf" \({AUDIT_SEC}\s+{T_ID}(?:[^()]*)?\)", ""),
    # Audit-section failure-numbered variant: (audit §27 failure #1)
    (rf" \({AUDIT_SEC}\s+failure\s+#\d+\)", ""),
    # Standalone audit-row, audit-section, audit-§
    (rf" \({AUDIT_ROW}(?:[^()]*)?\)", ""),
    (rf" \({AUDIT_SECTION}\)", ""),
    (rf" \({AUDIT_SEC}\)", ""),
    # Standalone T-tier and PR-T (must come last to avoid stealing combined matches)
    (rf" \({T_ID}\)", ""),
    (rf" \({PR_T_ID}\)", ""),
    (rf" \({PHASE_T_ID}\)", ""),
    (rf" \({PHASE_F_ID}\)", ""),
    # CLI UX audit row IDs in parentheticals (cli-ux-audit.md):
    # ``(I1)``, ``(I3, I4)``, ``(C1)``, ``(M2)``, etc.
    (rf" \({CLI_UX_ROW_ID}\)", ""),
    # Phase task ID paired with a CLI-UX row ID, with or without leading
    # ``-`` and with a ``/`` separator: ``(P5.T2 / I7)``, ``(M2 / P5.T3)``.
    (rf" \({PHASE_T_ID}\s*/\s*{CLI_UX_ROW_ID}\)", ""),
    (rf" \({CLI_UX_ROW_ID}\s*/\s*{PHASE_T_ID}\)", ""),
    # Multi-section audit references: ``(audit §§13, 15, 16, 21)``.
    (r" \(audit §§\d+(?:,\s*\d+)*\)", ""),
]


# Whole-sentence patterns that read as meta-commentary about the audit tags.
SENTENCE_PATTERNS: list[tuple[str, str]] = [
    (
        r"\s*Per-arc audit IDs \([^)]*\) are noted in parentheses on each non-cli-ux entry\.",
        "",
    ),
    (
        r"\s*Audit-row IDs from `\.sisyphus/plans/cli-ux-audit\.md` \([^)]*\) are noted in parentheses on each entry\.",
        "",
    ),
]


# ---------------------------------------------------------------------------
# Phase scopes
# ---------------------------------------------------------------------------

PHASE_1_FILES: list[str] = [
    # src/notebooklm/**/*.py
    "src/notebooklm/__init__.py",
    "src/notebooklm/_artifacts.py",
    "src/notebooklm/_auth/cookie_policy.py",
    "src/notebooklm/_auth/storage.py",
    "src/notebooklm/_idempotency.py",
    "src/notebooklm/_logging.py",
    "src/notebooklm/_mind_map.py",
    "src/notebooklm/_notebooks.py",
    "src/notebooklm/_research.py",
    "src/notebooklm/_sources.py",
    "src/notebooklm/auth.py",
    "src/notebooklm/cli/artifact_cmd.py",
    "src/notebooklm/cli/chat_cmd.py",
    "src/notebooklm/cli/download_cmd.py",
    "src/notebooklm/cli/error_handler.py",
    "src/notebooklm/cli/generate_cmd.py",
    "src/notebooklm/cli/helpers.py",
    "src/notebooklm/cli/note_cmd.py",
    "src/notebooklm/cli/notebook_cmd.py",
    "src/notebooklm/cli/options.py",
    "src/notebooklm/cli/research_cmd.py",
    "src/notebooklm/cli/session_cmd.py",
    "src/notebooklm/cli/source_cmd.py",
    "src/notebooklm/notebooklm_cli.py",
    "src/notebooklm/client.py",
    "src/notebooklm/exceptions.py",
    "src/notebooklm/types.py",
    # docs (user-facing).
    "docs/python-api.md",
    "docs/development.md",
    "docs/auth-cookie-lifecycle.md",
    "docs/cli-exit-codes.md",
    "docs/cli-reference.md",
    # root
    "CHANGELOG.md",
    "pyproject.toml",
    "CLAUDE.md",
]


PHASE_2_FILES: list[str] = [
    # Top-level test helpers.
    "tests/cassette_patterns.py",
    "tests/conftest.py",
    "tests/vcr_config.py",
    # Integration top-level.
    "tests/integration/conftest.py",
    "tests/integration/README.md",
    "tests/integration/test_artifacts_integration.py",
    "tests/integration/test_auth_refresh_vcr.py",
    "tests/integration/test_auto_refresh.py",
    "tests/integration/test_chat_multi_source_vcr.py",
    "tests/integration/test_empty_results_vcr.py",
    "tests/integration/test_error_paths_vcr.py",
    "tests/integration/test_mind_map_chain_vcr.py",
    "tests/integration/test_notebooks_integration.py",
    "tests/integration/test_notes_integration.py",
    "tests/integration/test_polling_vcr.py",
    "tests/integration/test_research_deep_poll_vcr.py",
    "tests/integration/test_save_chat_as_note_integration.py",
    "tests/integration/test_settings_integration.py",
    "tests/integration/test_settings_vcr.py",
    "tests/integration/test_sharing_integration.py",
    "tests/integration/test_sources_integration.py",
    "tests/integration/test_vcr_comprehensive.py",
    "tests/integration/test_vcr_example.py",
    "tests/integration/test_vcr_real_api.py",
    "tests/integration/test_workflow_tracer_vcr.py",
    # CLI VCR.
    "tests/integration/cli_vcr/conftest.py",
    "tests/integration/cli_vcr/test_artifacts.py",
    "tests/integration/cli_vcr/test_chat.py",
    "tests/integration/cli_vcr/test_doctor.py",
    "tests/integration/cli_vcr/test_downloads.py",
    "tests/integration/cli_vcr/test_generate.py",
    "tests/integration/cli_vcr/test_login_browser_cookies.py",
    "tests/integration/cli_vcr/test_notebooks.py",
    "tests/integration/cli_vcr/test_notes.py",
    "tests/integration/cli_vcr/test_profile.py",
    "tests/integration/cli_vcr/test_settings.py",
    "tests/integration/cli_vcr/test_sources.py",
    # Concurrency integration.
    "tests/integration/concurrency/conftest.py",
    "tests/integration/concurrency/helpers.py",
    "tests/integration/concurrency/README.md",
    "tests/integration/concurrency/test_add_file_toctou.py",
    "tests/integration/concurrency/test_aexit_exception_masking.py",
    "tests/integration/concurrency/test_artifact_poll_dedupe.py",
    "tests/integration/concurrency/test_auth_snapshot_torn_read.py",
    "tests/integration/concurrency/test_chat_history_race.py",
    "tests/integration/concurrency/test_cross_loop_affinity.py",
    "tests/integration/concurrency/test_download_blocks_loop.py",
    "tests/integration/concurrency/test_harness_smoke.py",
    "tests/integration/concurrency/test_helpers.py",
    "tests/integration/concurrency/test_idempotency_create.py",
    "tests/integration/concurrency/test_keepalive_path_canonicalize.py",
    "tests/integration/concurrency/test_max_concurrent_rpcs.py",
    "tests/integration/concurrency/test_note_create_cancel.py",
    "tests/integration/concurrency/test_pool_tuning.py",
    "tests/integration/concurrency/test_rate_limit_default.py",
    "tests/integration/concurrency/test_refresh_cancellation_propagation.py",
    "tests/integration/concurrency/test_refresh_cmd_race.py",
    "tests/integration/concurrency/test_research_task_crosswire.py",
    "tests/integration/concurrency/test_upload_blocks_loop.py",
    "tests/integration/concurrency/test_upload_cancel_dangling_session.py",
    "tests/integration/concurrency/test_upload_timeout_config.py",
    "tests/integration/concurrency/test_wait_for_sources_leak.py",
    # tests/scripts (test-adjacent helpers).
    "tests/scripts/check_cassettes_clean.py",
    "tests/scripts/check_method_coverage.py",
    "tests/scripts/compress_polling_cassette.py",
    "tests/scripts/setup-generation-notebook.py",
    # Unit conftest.
    "tests/unit/conftest.py",
    # Unit CLI tests.
    "tests/unit/cli/conftest.py",
    "tests/unit/cli/test_agent.py",
    "tests/unit/cli/test_artifact.py",
    "tests/unit/cli/test_chat.py",
    "tests/unit/cli/test_cli_contract.py",
    "tests/unit/cli/test_completion.py",
    "tests/unit/cli/test_doctor.py",
    "tests/unit/cli/test_download.py",
    "tests/unit/cli/test_encoding.py",
    "tests/unit/cli/test_error_handler.py",
    "tests/unit/cli/test_generate.py",
    "tests/unit/cli/test_grouped.py",
    "tests/unit/cli/test_help_text.py",
    "tests/unit/cli/test_helpers.py",
    "tests/unit/cli/test_language.py",
    "tests/unit/cli/test_note.py",
    "tests/unit/cli/test_notebook.py",
    "tests/unit/cli/test_profile.py",
    "tests/unit/cli/test_prompt_file.py",
    "tests/unit/cli/test_research.py",
    "tests/unit/cli/test_resolve.py",
    "tests/unit/cli/test_root_group.py",
    "tests/unit/cli/test_share.py",
    "tests/unit/cli/test_skill.py",
    "tests/unit/cli/test_source.py",
    "tests/unit/cli/test_storage_context_isolation.py",
    "tests/unit/cli/test_use_fails_closed.py",
    # Unit concurrency tests.
    "tests/unit/concurrency/test_auth_load_blocks_loop.py",
    "tests/unit/concurrency/test_close_cancellation_leak.py",
    "tests/unit/concurrency/test_download_collision.py",
    # Unit tests (top-level).
    "tests/unit/test_api_coverage.py",
    "tests/unit/test_artifact_downloads.py",
    "tests/unit/test_artifact_type_code_consistency.py",
    "tests/unit/test_artifacts_coverage.py",
    "tests/unit/test_artifacts_helpers.py",
    "tests/unit/test_artifacts_mind_map_injection.py",
    "tests/unit/test_artifacts_polling_retries.py",
    "tests/unit/test_atomic_io.py",
    "tests/unit/test_atomic_update_json.py",
    "tests/unit/test_auth_cookie_save_race.py",
    "tests/unit/test_auth_session.py",
    "tests/unit/test_backoff.py",
    "tests/unit/test_cassette_patterns.py",
    "tests/unit/test_cassette_sanitizer.py",
    "tests/unit/test_chat.py",
    "tests/unit/test_chat_ask_invariants.py",
    "tests/unit/test_chat_characterization.py",
    "tests/unit/test_chat_error_payload.py",
    "tests/unit/test_chat_helpers.py",
    "tests/unit/test_chat_history.py",
    "tests/unit/test_chat_references.py",
    "tests/unit/test_check_coverage_thresholds.py",
    "tests/unit/test_check_rpc_health.py",
    "tests/unit/test_chromium_profiles.py",
    "tests/unit/test_ci_audit_scripts.py",
    "tests/unit/test_ci_install_parity.py",
    "tests/unit/test_claude_md_freshness.py",
    "tests/unit/test_cli_source_delete.py",
    "tests/unit/test_client.py",
    "tests/unit/test_client_keepalive.py",
    "tests/unit/test_concurrency_cache_race.py",
    "tests/unit/test_concurrency_refresh_race.py",
    "tests/unit/test_conversation.py",
    "tests/unit/test_cookie_domain_split.py",
    "tests/unit/test_cookie_redaction.py",
    "tests/unit/test_decoder.py",
    "tests/unit/test_docstrings.py",
    "tests/unit/test_download_helpers.py",
    "tests/unit/test_download_result.py",
    "tests/unit/test_download_url.py",
    "tests/unit/test_e2e_conftest_options.py",
    "tests/unit/test_encoder.py",
    "tests/unit/test_env.py",
    "tests/unit/test_env_base_url.py",
    "tests/unit/test_exceptions.py",
    "tests/unit/test_firefox_containers.py",
    "tests/unit/test_init_order.py",
    "tests/unit/test_json_error_exit.py",
    "tests/unit/test_json_stdout_purity.py",
    "tests/unit/test_logging.py",
    "tests/unit/test_logging_correlation.py",
    "tests/unit/test_migration.py",
    "tests/unit/test_migration_lock.py",
    "tests/unit/test_notebook_api.py",
    "tests/unit/test_notebook_metadata.py",
    "tests/unit/test_notebooks_extractors.py",
    "tests/unit/test_notes_unit.py",
    "tests/unit/test_observability.py",
    "tests/unit/test_paths.py",
    "tests/unit/test_public_shims.py",
    "tests/unit/test_quota_failure_detection.py",
    "tests/unit/test_rate_limit_retry.py",
    "tests/unit/test_refresh_cmd_shlex.py",
    "tests/unit/test_refresh_lock_lazy_init.py",
    "tests/unit/test_refresh_lock_registry.py",
    "tests/unit/test_refresh_state_machine.py",
    "tests/unit/test_research.py",
    "tests/unit/test_research_api.py",
    "tests/unit/test_rescrub_cassettes_script.py",
    "tests/unit/test_response_preview.py",
    "tests/unit/test_retry_after.py",
    "tests/unit/test_rpc_health_coverage.py",
    "tests/unit/test_rpc_overrides.py",
    "tests/unit/test_rpc_types.py",
    "tests/unit/test_safe_index.py",
    "tests/unit/test_save_chat_as_note_encoder.py",
    "tests/unit/test_save_lock_contract.py",
    "tests/unit/test_select_artifact.py",
    "tests/unit/test_sharing_manager.py",
    "tests/unit/test_sharing_types.py",
    "tests/unit/test_source_add_service.py",
    "tests/unit/test_source_content_renderer.py",
    "tests/unit/test_source_listing_service.py",
    "tests/unit/test_source_polling_service.py",
    "tests/unit/test_source_selection.py",
    "tests/unit/test_source_status.py",
    "tests/unit/test_source_symlink.py",
    "tests/unit/test_source_upload_pipeline.py",
    "tests/unit/test_sources_upload.py",
    "tests/unit/test_streaming_chat_wire.py",
    "tests/unit/test_swallow_observability.py",
    "tests/unit/test_tier_enforcement_hook.py",
    "tests/unit/test_token_regex.py",
    "tests/unit/test_types.py",
    "tests/unit/test_url_utils.py",
    "tests/unit/test_user_settings_api.py",
    "tests/unit/test_vcr_config.py",
    "tests/unit/test_version_check.py",
    "tests/unit/test_version_gate.py",
    "tests/unit/test_warning_dedupe.py",
    "tests/unit/test_windows_compatibility.py",
    "tests/unit/test_with_client_handle_errors.py",
    "tests/unit/test_youtube_extraction.py",
]


PHASE_SCOPES: dict[str, list[str]] = {
    "phase1": PHASE_1_FILES,
    "phase2": PHASE_2_FILES,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def transform_text(text: str) -> str:
    out = text
    for pat, repl in PAREN_PATTERNS:
        out = re.sub(pat, repl, out)
    for pat, repl in SENTENCE_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in PHASE_SCOPES:
        valid_phases = "|".join(sorted(PHASE_SCOPES.keys()))
        print(f"usage: {argv[0]} {{{valid_phases}}}", file=sys.stderr)
        return 2

    phase = argv[1]
    targets = PHASE_SCOPES[phase]

    changed: list[Path] = []
    missing: list[str] = []
    for rel in targets:
        path = REPO_ROOT / rel
        if not path.exists():
            missing.append(rel)
            continue
        text = path.read_text(encoding="utf-8")
        new = transform_text(text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path)

    if missing:
        print(f"WARNING: {len(missing)} target paths missing (skipped):")
        for m in missing:
            print(f"  {m}")

    print(f"Edited {len(changed)} files:")
    for p in sorted(changed):
        print(f"  {p.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
