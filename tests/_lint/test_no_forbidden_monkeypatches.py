"""Meta-lint enforcing the test-monkeypatch policy from ADR-007.

This test scans every ``.py`` file under ``tests/`` for the four
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

3. **Direct attribute assignment of ``AsyncMock`` to the RPC/
   transport surface** — mutates an instance instead of injecting at
   construction. Caught with a negative-lookbehind so chained forms like
   ``self._client._target.rpc_call = AsyncMock(...)`` are also reported.

   .. code-block:: python

       target.rpc_call = AsyncMock(return_value=None)

4. **``unittest.mock`` string-target patches into private internals** —
   ``mock.patch("notebooklm._private…")`` / ``patch("notebooklm._private…")``
   / ``patch.object(notebooklm._private…, ...)``. Same import-string failure
   mode as (1), but routed through ``unittest.mock`` instead of
   ``monkeypatch`` — the channel where the growth happened and which the lint
   previously missed entirely (issue #1325). Scoped to private
   ``notebooklm._*`` paths: those are the implementation internals the policy
   forbids reaching into, and they silently no-op when the attribute relocates.

   .. code-block:: python

       mock.patch("notebooklm._research.ResearchAPI._poll", fake)
       patch("notebooklm._artifact_downloads.httpx", fake)

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
# the dotted prefix goes (``target.rpc_call`` and
# ``self._client._target.rpc_call`` both fire). Without the lookbehind,
# regex backtracking could shorten the prefix and create overlapping
# matches; with it, each occurrence is reported once with the natural
# start position.
_PATTERN_ASYNCMOCK_ASSIGN = re.compile(
    # Method-name enumeration kept INTENTIONALLY broad — not narrowed to
    # only the methods that still exist on ``Session`` (per gemini-code-
    # assist's review on PR #1078 / Wave 11c). The lint exists precisely
    # to catch dynamic attribute assignment of ``AsyncMock`` onto a fake
    # or duck-typed collaborator — those targets are bag-of-attributes
    # fakes (``MagicMock``, ``FakeSession``) that happily accept *any*
    # attribute name regardless of whether the production class still
    # defines it. Removing a deleted method name from this enumeration
    # would create a silent escape hatch: a test that re-introduces the
    # forbidden ``<chain>.transport_post = AsyncMock(...)`` pattern
    # against a ``MagicMock(spec=...)`` would no longer surface, even
    # though that is exactly the ADR-007 violation the lint is supposed
    # to catch. ``rpc_call`` is the canonical core-RPC seam; the
    # transport-side names retained here
    # (``transport_post`` / ``_perform_authed_post`` / ``next_reqid`` /
    # ``save_cookies``) were deleted from ``Session`` in Waves 11a-11c
    # but remain in this enumeration so the lint keeps catching dynamic
    # re-assignment of them on a fake.
    r"(?<![\w.])[\w.]+\.(?:rpc_call|transport_post|_perform_authed_post|next_reqid|save_cookies)\s*=\s*(?:[\w]+\.)*AsyncMock"
)

# (d) ``mock.patch("notebooklm._private…")`` / ``patch("notebooklm._private…")``
#     — ``unittest.mock`` string-target patch into a *private* internal path.
#
# The ``(?<![\w.])(?:[\w]+\.)*`` prefix anchors ``patch`` at a word boundary
# and allows an optional dotted module qualifier, so the bare ``patch(`` (from
# ``from unittest.mock import patch``), ``mock.patch(``, and
# ``unittest.mock.patch(`` forms all match, while ``monkeypatch(`` / ``dispatch(``
# (where ``patch`` is preceded by a word char) and ``patch.object(`` (no ``(``
# immediately after ``patch``) do not. Scoped to ``notebooklm\._`` so only
# *private* targets are flagged — patches at public facades are out of scope for
# this rule (issue #1325).
_PATTERN_MOCK_PATCH_PRIVATE = re.compile(r"(?<![\w.])(?:[\w]+\.)*patch\(\s*[\"']notebooklm\._")

# (e) ``patch.object(notebooklm._private…, "attr", …)`` — the object-target
#     ``unittest.mock`` form aimed at a private module reference. No occurrences
#     exist today; the rule guards against regressions on this second
#     ``unittest.mock`` shape.
_PATTERN_MOCK_PATCH_OBJECT_PRIVATE = re.compile(
    r"(?<![\w.])(?:[\w]+\.)*patch\.object\(\s*[\w.]*notebooklm\._"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("string-target monkeypatch (forbidden by ADR-007)", _PATTERN_STRING_TARGET),
    ("object-attribute monkeypatch (forbidden by ADR-007)", _PATTERN_OBJECT_ATTR),
    ("AsyncMock attribute assignment (forbidden by ADR-007)", _PATTERN_ASYNCMOCK_ASSIGN),
    ("mock.patch string-target into private (forbidden by ADR-007)", _PATTERN_MOCK_PATCH_PRIVATE),
    ("patch.object into private module (forbidden by ADR-007)", _PATTERN_MOCK_PATCH_OBJECT_PRIVATE),
)


# ---------------------------------------------------------------------------
# File-level allowlist — baked at PR-start (2026-05-18). Shrinks across
# D1 PR-2 and D1 PR-3; target end state is an empty set.
#
# A second batch (issue #1325) was added when the lint was extended to also
# catch ``mock.patch("notebooklm._private…")`` / ``patch.object`` string targets
# into private internals — a previously-unpoliced channel. Those entries are
# grouped under the "issue #1325" header below and shrink toward zero on the
# same terms: a file whose offenders are migrated to seams must be removed from
# the allowlist (a stale entry fails the lint).
# ---------------------------------------------------------------------------

_ALLOWLIST: frozenset[str] = frozenset(
    {
        # CLI VCR test patches `notebooklm.cli.services.login.refresh.*` and
        # `notebooklm.auth.account.*` module-level seams (browser-cookie
        # extraction, account profile loaders) — these are CLI-side seams above
        # the `NotebookLMClient` core that `make_fake_core(...)` covers.
        # reason: CLI-side module seam — out of scope for `make_fake_core` (core-injection only)
        "tests/integration/cli_vcr/test_login_browser_cookies.py",
        # reason: loop-affinity violation test — raw patch required
        "tests/integration/concurrency/test_aexit_exception_masking.py",
        # reason: loop-affinity violation test — raw patch required
        "tests/integration/concurrency/test_download_blocks_loop.py",
        # reason: loop-affinity violation test — raw patch required
        "tests/integration/concurrency/test_idempotency_create.py",
        # reason: loop-affinity violation test — raw patch required
        "tests/integration/concurrency/test_upload_blocks_loop.py",
        # reason: loop-affinity violation test — raw patch required
        "tests/integration/concurrency/test_upload_cancel_dangling_session.py",
        # reason: integration test patches transport-side
        # `notebooklm.<module>.*` stdlib seams (httpx-level overrides for
        # side-effects/idempotency cassettes); these patches sit below the
        # core-injection seam that `make_fake_core` covers.
        "tests/integration/test_side_effects_idempotency.py",
        # reason: integration test patches transport-side `notebooklm.<module>.*`
        # stdlib seams (httpx-level overrides for sources idempotency cassettes);
        # below the core-injection seam covered by `make_fake_core`.
        "tests/integration/test_sources_idempotency.py",
        # reason: CLI conftest patches `notebooklm.cli.*` module-level seams
        # (resolvers, click context shims) above the `NotebookLMClient` core
        # that `make_fake_core(...)` covers.
        "tests/unit/cli/conftest.py",
        # reason: download-collision concurrency test exercises raw Session-attribute
        # mutation to provoke download-id collision races; not a candidate for
        # constructor injection since the test is *about* attribute-level races.
        "tests/unit/concurrency/test_download_collision.py",
        # reason: public API coverage smoke-test imports `notebooklm.<feature>`
        # facades to assert re-export shapes; the string-target patches verify
        # the facade itself, not the core surface.
        "tests/unit/test_api_coverage.py",
        # reason: cookie save-race test patches module-level
        # `_try_claim_rotation`, `_file_lock_try_exclusive`,
        # `save_cookies_to_storage` rotation/lock helpers — outside the
        # core-injection surface.
        "tests/unit/test_auth_cookie_save_race.py",
        # reason: PSIDTS inline recovery (issue #865) patches module-level
        # rotation/lock seams (`_try_claim_rotation`, `_file_lock_try_exclusive`,
        # `save_cookies_to_storage`, `_load_storage_state`, `get_storage_path`)
        # — outside the core-injection surface `make_fake_core` covers.
        "tests/unit/test_auth_psidts_recovery.py",
        # reason: RPC executor unit test stub-patches `notebooklm._rpc_executor`
        # module-level stdlib seams (asyncio.sleep, time providers) on the
        # executor module itself — below the core-injection seam.
        "tests/unit/test_rpc_executor.py",
        # reason: authed-post pipeline test patches `notebooklm._streaming_post`
        # and `notebooklm._transport_errors` module-level stdlib seams (httpx
        # response builders, time/retry helpers) — transport-layer seams below
        # the core-injection surface.
        "tests/unit/test_authed_post_pipeline.py",
        # reason: Firefox container detection test patches module-level
        # `notebooklm.cli.services.login.firefox_accounts.*` filesystem and
        # database-discovery helpers — CLI-side seam outside `make_fake_core`.
        "tests/unit/test_firefox_containers.py",
        # reason: client __init__ ordering test patches construction helpers to
        # assert wiring order — verifies construction sequencing, not a core method seam.
        "tests/unit/test_init_order.py",
        # reason: public-API shim test asserts forwarding of `notebooklm.<x>`
        # facades; the string-target patches *are* the test subject (shim
        # routing) rather than an incidental implementation detail.
        "tests/unit/test_public_shims.py",
        # reason: RPC overrides test patches `notebooklm.rpc.types.RPC_METHOD_OVERRIDES`
        # module-level mapping used during request encoding — module-level data
        # seam, not a core attribute.
        "tests/unit/test_rpc_overrides.py",
        # -------------------------------------------------------------------
        # issue #1325: pre-existing `mock.patch("notebooklm._private…")`
        # string-target offenders, surfaced when the lint's coverage was
        # extended to the `unittest.mock` channel. These reach into private
        # `notebooklm._*` modules and must migrate to constructor seams /
        # public hooks; this list shrinks toward zero.
        # (`tests/integration/concurrency/test_download_blocks_loop.py` also
        # matches this rule but is already allowlisted above.)
        # -------------------------------------------------------------------
        # reason: patches `notebooklm._runtime_init` construction internals to
        # assert httpx connection-pool tuning — runtime seam below the
        # core-injection surface.
        "tests/integration/concurrency/test_pool_tuning.py",
        # reason: patches `notebooklm._sources` internals to assert upload
        # timeout config — service seam, not a core attribute.
        "tests/integration/concurrency/test_upload_timeout_config.py",
        # reason: patches `notebooklm._artifact_downloads` download-coordinator
        # internals (httpx-level) for the artifacts integration cassette.
        "tests/integration/test_artifacts_integration.py",
        # reason: patches `notebooklm._sources` source-addition internals for
        # the sources integration cassette.
        "tests/integration/test_sources_integration.py",
        # reason: patches `notebooklm._artifact_downloads` download-coordinator
        # internals to exercise the asynchronous download path.
        "tests/unit/test_artifact_downloads.py",
        # reason: patches `notebooklm._artifact_downloads` internals for artifact
        # coverage edge cases.
        "tests/unit/test_artifacts_coverage.py",
        # reason: patches `notebooklm._artifact_downloads` internals to assert
        # download-result shaping.
        "tests/unit/test_download_result.py",
        # reason: patches `notebooklm._artifact_downloads` internals to assert
        # download-URL resolution.
        "tests/unit/test_download_url.py",
        # reason: patches `notebooklm._deadline` retry/backoff timing internals
        # to assert rate-limit retry behaviour.
        "tests/unit/test_rate_limit_retry.py",
        # reason: patches `notebooklm._research` research-flow internals to
        # exercise import-with-verification — the largest single concentration
        # of private-target patches (issue #1325).
        "tests/unit/test_research_import_with_verification.py",
        # reason: patches `notebooklm._sources` poll-coordinator internals
        # for the source polling service.
        "tests/unit/test_source_polling_service.py",
        # reason: patches `notebooklm._sources` internals to assert source
        # status transitions.
        "tests/unit/test_source_status.py",
        # reason: patches `notebooklm._source_upload` internals for upload
        # coverage edge cases.
        "tests/unit/test_source_upload_coverage.py",
        # reason: patches `notebooklm._types` dataclass internals exercised
        # through the public types surface.
        "tests/unit/test_types.py",
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
