"""Meta-lint enforcing the test-monkeypatch policy from ADR-007.

This test scans every ``.py`` file under ``tests/`` for the three
forbidden patterns documented in
``docs/adr/0007-test-monkeypatch-policy.md`` and fails if any file *not*
on the shrinking allowlist contains a match.

Forbidden patterns
------------------

1. **String-target patches into ``notebooklm.*``** — relies on import
   string resolution; silently no-ops when storage relocates.

   .. code-block:: python

       monkeypatch.setattr("notebooklm.auth.get_storage_path", fake)

2. **Object-attribute patches via the imported ``notebooklm`` module** —
   same failure mode, different syntax.

   .. code-block:: python

       monkeypatch.setattr(notebooklm._core, "asyncio", fake_asyncio)

3. **Direct attribute assignment of ``AsyncMock`` to the core's RPC/
   transport surface** — mutates an instance instead of injecting at
   construction. Caught with a negative-lookbehind so chained forms like
   ``self._client._core.rpc_call = AsyncMock(...)`` are also reported.

   .. code-block:: python

       core.rpc_call = AsyncMock(return_value=None)

Allowlist
---------

``_ALLOWLIST`` enumerates the files that *currently* contain at least
one of the forbidden patterns at PR-1's HEAD. The list shrinks as
D1 PR-2 (auth-side migration) and D1 PR-3 (CLI-side migration) retire
offenders. Once the list is empty, the per-file gate becomes a global
invariant.

The allowlist is file-level, not site-level (line-number-level), so it
survives rebases and reorderings without spurious churn. See
ADR-007 "Alternatives considered: per-site allowlist entries".

A few path conventions:

- Paths are stored relative to the repository root and use ``/`` as the
  separator on every platform so the test runs deterministically on
  Linux, macOS, and Windows CI.
- The allowlist enforces *exact* membership: a file on the allowlist
  that has had its offenders cleaned up triggers a failure, signaling
  that the entry should be removed (otherwise the lint silently rots).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_ROOT.parent

# Skip these subtrees:
#  - ``tests/_lint``: this file itself contains the regex literals as
#    string data; matching them would be a false positive.
#  - ``tests/_fixtures``: the policy's substrate; tests inside use the
#    factory directly and do not (and must not) demonstrate the forbidden
#    patterns.
#  - ``tests/cassettes``, ``tests/fixtures``: data-only directories
#    containing VCR cassettes and HTML/JSON fixtures, no Python source.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "_lint",
        "_fixtures",
        "cassettes",
        "fixtures",
    }
)


# ---------------------------------------------------------------------------
# Forbidden patterns (regex set)
# ---------------------------------------------------------------------------

# (a) ``monkeypatch.setattr("notebooklm.X.Y", ...)`` — string-target form.
_PATTERN_STRING_TARGET = re.compile(r"monkeypatch\.setattr\(\s*[\"']notebooklm\.")

# (b) ``monkeypatch.setattr(notebooklm.X, "attr", ...)`` — attribute-of-imported-module form.
_PATTERN_OBJECT_ATTR = re.compile(r"monkeypatch\.setattr\(\s*notebooklm\.")

# (c) ``<chain>.<core-method> = AsyncMock(...)`` — direct attribute assignment.
#
# The negative-lookbehind ``(?<![\w.])`` ensures the matched chain *starts*
# at a word boundary, so we match the full chain regardless of how deep
# the dotted prefix goes (``core.rpc_call`` and
# ``self._client._core.rpc_call`` both fire). Without the lookbehind,
# regex backtracking could shorten the prefix and create overlapping
# matches; with it, each occurrence is reported once with the natural
# start position.
_PATTERN_ASYNCMOCK_ASSIGN = re.compile(
    # Wave 11c of session-decoupling deleted ``Session._perform_authed_post``
    # along with the ``_begin_transport_*`` / ``_finish_transport_*`` legacy
    # names — once a method no longer exists on ``Session``, attempting to
    # patch it with ``AsyncMock`` would surface immediately at runtime as an
    # ``AttributeError``, so the lint pattern stops enumerating them.
    r"(?<![\w.])[\w.]+\.(rpc_call)\s*=\s*(?:[\w]+\.)*AsyncMock"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("string-target monkeypatch (forbidden by ADR-007)", _PATTERN_STRING_TARGET),
    ("object-attribute monkeypatch (forbidden by ADR-007)", _PATTERN_OBJECT_ATTR),
    ("AsyncMock attribute assignment (forbidden by ADR-007)", _PATTERN_ASYNCMOCK_ASSIGN),
)


# ---------------------------------------------------------------------------
# File-level allowlist — baked at PR-start (2026-05-18). Shrinks across
# D1 PR-2 and D1 PR-3; target end state is an empty set.
# ---------------------------------------------------------------------------

_ALLOWLIST: frozenset[str] = frozenset(
    {
        "tests/integration/cli_vcr/test_login_browser_cookies.py",
        "tests/integration/concurrency/test_aexit_exception_masking.py",
        "tests/integration/concurrency/test_download_blocks_loop.py",
        "tests/integration/concurrency/test_idempotency_create.py",
        "tests/integration/concurrency/test_upload_blocks_loop.py",
        "tests/integration/concurrency/test_upload_cancel_dangling_session.py",
        "tests/integration/test_artifacts_integration.py",
        "tests/integration/test_side_effects_idempotency.py",
        "tests/integration/test_sources_idempotency.py",
        "tests/unit/test_artifacts_drift.py",
        "tests/unit/test_get_summary_drift.py",
        "tests/unit/cli/conftest.py",
        "tests/unit/concurrency/test_download_collision.py",
        "tests/unit/test_api_coverage.py",
        "tests/unit/test_artifact_downloads.py",
        "tests/unit/test_artifacts_coverage.py",
        "tests/unit/test_auth_cookie_save_race.py",
        # PSIDTS inline recovery (issue #865) — patches module-level
        # seams (``_try_claim_rotation``, ``_file_lock_try_exclusive``,
        # ``save_cookies_to_storage``, ``_load_storage_state``, and
        # ``get_storage_path``) that are NOT part of the core-injection
        # surface ``tests/_fixtures/make_fake_core(...)`` covers. Same
        # rationale as the neighboring ``test_auth_cookie_save_race.py``
        # and ``test_auth_session.py`` entries; revisit when ADR-007's
        # seam-substitution pattern is extended to cover module-level
        # rotation/lock helpers.
        "tests/unit/test_auth_psidts_recovery.py",
        "tests/unit/test_backoff.py",
        # Phase 2 PR 5 migrated this file's ``asyncio.to_thread`` patch
        # off the legacy ``notebooklm._core.asyncio.to_thread`` shim onto
        # its canonical importing module
        # (``notebooklm._session_lifecycle.asyncio.to_thread``, where
        # ``ClientLifecycle.save_cookies`` sources it). The new patch
        # target is still a string-target into the ``notebooklm.*``
        # namespace, so the file lands on the allowlist with the rest of
        # the stdlib-seam patchers (``test_authed_post_pipeline.py``,
        # ``test_rpc_executor.py``, ``test_side_effects_idempotency.py``,
        # …) until ADR-007's pattern is extended to stdlib seams.
        "tests/unit/test_cookie_persistence.py",
        # ``tests/unit/test_session_lifecycle.py`` removed from allowlist in
        # Phase 4 (v0.5.0): the file's monkeypatch.setattr sites that targeted
        # ``notebooklm._core.*`` were retargeted to the canonical seams
        # (_auth.storage / _auth.keepalive / _error_injection) when the
        # ``_core`` compatibility shim was deleted.
        "tests/unit/test_rpc_executor.py",
        "tests/unit/test_authed_post_pipeline.py",
        "tests/unit/test_download_url.py",
        "tests/unit/test_firefox_containers.py",
        # P3.T1 generate-extraction service tests stub the CLI resolver
        # functions (``notebooklm.cli.resolve.resolve_notebook_id`` and
        # ``resolve_source_ids``) via ``monkeypatch.setattr`` string targets.
        # Those resolvers are module-level CLI seams above the
        # ``NotebookLMClient`` core that ``make_fake_core(...)`` covers, so
        # the same string-target pattern that ``test_public_shims.py`` uses
        # for its non-core seams is required here. Revisit when ADR-007's
        # seam-substitution pattern is extended to cover the CLI resolver
        # surface.
        "tests/unit/test_generate_service.py",
        # tests/unit/test_idempotency_registry.py — removed in Wave 4 of
        # session-decoupling: the test's RpcExecutor construction was
        # migrated from a MagicMock-owner monkeypatch pattern to the
        # ADR-014 Rule 5 keyword-collaborator constructor shape.
        "tests/unit/test_init_order.py",
        "tests/unit/test_migration_lock.py",
        "tests/unit/test_notebook_api.py",
        "tests/unit/test_notes_unit.py",
        "tests/unit/test_public_shims.py",
        "tests/unit/test_quota_failure_detection.py",
        "tests/unit/test_rpc_overrides.py",
        "tests/unit/test_select_artifact.py",
        "tests/unit/test_sharing_manager.py",
        "tests/unit/test_sharing_types.py",
        "tests/unit/test_source_selection.py",
        "tests/unit/test_source_symlink.py",
        "tests/unit/test_sources_upload.py",
        "tests/unit/test_swallow_observability.py",
        "tests/unit/test_user_settings_api.py",
    }
)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if rel_parts and rel_parts[0] in _SKIP_DIRS:
            continue
        yield path


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return ``[(line_no, pattern_label), ...]`` for every match in *path*.

    Scans the file as a single string (not line-by-line) so multi-line
    forms like::

        monkeypatch.setattr(
            "notebooklm.auth.X",
            fake,
        )

    are caught. ``\\s`` already spans newlines in Python's regex engine,
    so no flag changes are needed — the regexes were authored against
    "any whitespace, including newlines" semantics.
    """
    findings: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for label, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            # Match starts can land at column 0 of a continuation line;
            # report the line where the *match* begins, which is also
            # the line a reader will scan first when chasing the error.
            line_no = text.count("\n", 0, match.start()) + 1
            findings.append((line_no, label))
    findings.sort()
    return findings


def _rel_posix(path: Path) -> str:
    """Return *path* as a repo-relative POSIX-style string."""
    return path.relative_to(_REPO_ROOT).as_posix()


def test_no_forbidden_monkeypatches_outside_allowlist() -> None:
    """No tests file outside the allowlist may contain the forbidden patterns.

    See ``docs/adr/0007-test-monkeypatch-policy.md``.
    """

    violations: list[tuple[str, int, str]] = []
    seen_files_with_findings: set[str] = set()

    for path in _iter_python_files(_TESTS_ROOT):
        findings = _scan_file(path)
        if not findings:
            continue
        rel = _rel_posix(path)
        seen_files_with_findings.add(rel)
        if rel in _ALLOWLIST:
            continue
        for line_no, label in findings:
            violations.append((rel, line_no, label))

    # Surface stale allowlist entries: a file that has been cleaned up
    # should be removed from the allowlist so the lint keeps tightening.
    stale = sorted(_ALLOWLIST - seen_files_with_findings)
    extra_messages: list[str] = []
    if stale:
        extra_messages.append(
            "Stale allowlist entries (no forbidden patterns found; remove from _ALLOWLIST):\n"
            + "\n".join(f"  - {entry}" for entry in stale)
        )

    if violations:
        formatted = "\n".join(
            f"  {file}:{line}  {label}" for file, line, label in sorted(violations)
        )
        msg = (
            "Forbidden test-monkeypatch patterns detected outside the "
            "ADR-007 allowlist. Migrate the test(s) to constructor "
            "injection via ``tests/_fixtures/make_fake_core(...)`` or, "
            "if migration must defer, add the file path to "
            "``tests/_lint/test_no_forbidden_monkeypatches.py::_ALLOWLIST`` "
            "with a justification in the PR description.\n\n"
            f"Violations ({len(violations)}):\n{formatted}"
        )
        if extra_messages:
            msg = msg + "\n\n" + "\n\n".join(extra_messages)
        raise AssertionError(msg)

    if stale:
        raise AssertionError("\n\n".join(extra_messages))
