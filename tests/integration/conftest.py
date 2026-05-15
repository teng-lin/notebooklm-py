"""Shared fixtures for integration tests."""

import importlib.util
from pathlib import Path

import pytest

from notebooklm.auth import AuthTokens

# Load ``tests/vcr_config.py`` by file path — the ``tests`` directory is not a
# package (no ``__init__.py``), so ``from tests.vcr_config import ...`` only
# works when the repo root happens to be on ``sys.path``. That holds in a
# fresh REPL but NOT inside pytest's per-module import. Loading by file path
# bypasses ``sys.path`` and is the same idiom used inside ``vcr_config.py``
# itself for its sibling ``cassette_patterns.py`` import.
_vcr_config_spec = importlib.util.spec_from_file_location(
    "tests_vcr_config", Path(__file__).resolve().parent.parent / "vcr_config.py"
)
assert _vcr_config_spec is not None and _vcr_config_spec.loader is not None, (
    "Could not load tests/vcr_config.py from tests/integration/conftest.py"
)
_vcr_config = importlib.util.module_from_spec(_vcr_config_spec)
_vcr_config_spec.loader.exec_module(_vcr_config)
_is_vcr_record_mode = _vcr_config._is_vcr_record_mode

# =============================================================================
# VCR Cassette Availability Check
# =============================================================================

CASSETTES_DIR = Path(__file__).parent.parent / "cassettes"

# Real cassettes live at the top level of ``tests/cassettes/``; illustrative
# fixtures (``example_*.yaml``) live in ``tests/cassettes/examples/`` per the
# naming convention documented in ``tests/cassettes/README.md`` (T8.E2).
#
# This filter decides whether the VCR integration tier has anything to replay:
# - Globbing ``*.yaml`` (non-recursive) naturally skips the ``examples/``
#   subdirectory, so example fixtures cannot inflate the "real cassettes
#   present" signal.
# - The ``startswith("example_")`` guard is retained as a belt-and-braces
#   filter — if a future contributor lands an ``example_*.yaml`` file at the
#   top level by mistake, it still won't count as a real recording.
_real_cassettes = (
    [f for f in CASSETTES_DIR.glob("*.yaml") if not f.name.startswith("example_")]
    if CASSETTES_DIR.exists()
    else []
)

# Skip VCR tests if no real cassettes exist (unless in record mode)
_vcr_record_mode = _is_vcr_record_mode()
_cassettes_available = bool(_real_cassettes) or _vcr_record_mode

# Marker for skipping VCR tests when cassettes are not available
skip_no_cassettes = pytest.mark.skipif(
    not _cassettes_available,
    reason="VCR cassettes not available. Set NOTEBOOKLM_VCR_RECORD=1 to record.",
)


async def get_vcr_auth() -> AuthTokens:
    """Get auth tokens for VCR tests.

    In record mode: loads real auth from storage (required for recording).
    In replay mode: returns mock auth (cassettes have recorded responses).
    """
    if _vcr_record_mode:
        return await AuthTokens.from_storage()
    else:
        # Mock auth for replay - values don't matter, VCR replays recorded responses
        return AuthTokens(
            cookies={
                "SID": "mock_sid",
                "HSID": "mock_hsid",
                "SSID": "mock_ssid",
                "APISID": "mock_apisid",
                "SAPISID": "mock_sapisid",
            },
            csrf_token="mock_csrf_token",
            session_id="mock_session_id",
        )


# =============================================================================
# T8.A1 — xfail cassettes whose recorded ``rpcids`` order does not match live
# call order under the new default matcher (``method, scheme, host, port,
# path, rpcids``). These cassettes were previously selected by play-count
# ordering alone; tightening the matcher in T8.A1 surfaces the drift.
# Each entry MUST be removed in its phase-2 cassette-repair PR (T8.B*).
# Tracking issue: tier-8-followup label.
# =============================================================================
_T8_A1_XFAIL_NODEIDS = frozenset(
    {
        # test_artifacts.py
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListCommand::test_artifact_list[False]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListCommand::test_artifact_list[True]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[quiz-artifacts_list_quizzes.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[report-artifacts_list_reports.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[video-artifacts_list_video.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[flashcard-artifacts_list_flashcards.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[infographic-artifacts_list_infographics.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[slide-deck-artifacts_list_slide_decks.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[data-table-artifacts_list_data_tables.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactListByType::test_artifact_list_by_type[mind-map-notes_list_mind_maps.yaml]",
        "tests/integration/cli_vcr/test_artifacts.py::TestArtifactSuggestionsCommand::test_artifact_suggestions",
        # test_chat.py
        "tests/integration/cli_vcr/test_chat.py::TestAskCommand::test_ask_question",
        "tests/integration/cli_vcr/test_chat.py::TestAskCommand::test_ask_question_json",
        "tests/integration/cli_vcr/test_chat.py::TestHistoryCommand::test_history",
        # test_generate.py
        "tests/integration/cli_vcr/test_generate.py::TestGenerateCommands::test_generate[quiz-artifacts_generate_quiz.yaml-extra_args0]",
        "tests/integration/cli_vcr/test_generate.py::TestGenerateCommands::test_generate[flashcards-artifacts_generate_flashcards.yaml-extra_args1]",
        "tests/integration/cli_vcr/test_generate.py::TestGenerateCommands::test_generate[report-artifacts_generate_report.yaml-extra_args2]",
        "tests/integration/cli_vcr/test_generate.py::TestGenerateCommands::test_generate[report-artifacts_generate_study_guide.yaml-extra_args3]",
        # ``test_revise_slide`` was repaired in T8.B1 — cassette re-recorded
        # against the live REVISE_SLIDE RPC.
        # test_notebooks.py
        "tests/integration/cli_vcr/test_notebooks.py::TestSummaryCommand::test_summary",
        # test_notes.py
        "tests/integration/cli_vcr/test_notes.py::TestNoteCommands::test_note_command[notes_list.yaml-args0]",
        "tests/integration/cli_vcr/test_notes.py::TestNoteCommands::test_note_command[notes_create.yaml-args1]",
        # test_sources.py
        "tests/integration/cli_vcr/test_sources.py::TestSourceListCommand::test_source_list[False]",
        "tests/integration/cli_vcr/test_sources.py::TestSourceListCommand::test_source_list[True]",
        "tests/integration/cli_vcr/test_sources.py::TestSourceAddCommand::test_source_add[sources_add_url.yaml-args0]",
        "tests/integration/cli_vcr/test_sources.py::TestSourceAddCommand::test_source_add[sources_add_text.yaml-args1]",
        "tests/integration/cli_vcr/test_sources.py::TestSourceContentCommands::test_source_content[guide-sources_get_guide.yaml]",
        "tests/integration/cli_vcr/test_sources.py::TestSourceContentCommands::test_source_content[fulltext-sources_get_fulltext.yaml]",
        # test_vcr_comprehensive.py
        "tests/integration/test_vcr_comprehensive.py::TestArtifactsListAPI::test_suggest_reports",
    }
)


def _has_use_cassette_decorator(item) -> bool:
    """Detect ``@notebooklm_vcr.use_cassette(...)`` on a test callable.

    ``VCR.use_cassette`` returns a ``CassetteContextDecorator``; applying it
    as a decorator wraps the test in a ``wrapt.FunctionWrapper`` whose
    ``_self_wrapper`` is a bound method on the ``CassetteContextDecorator``.
    We detect that wrapper class by name (``CassetteContextDecorator``) so
    the check stays robust if vcrpy ever moves the class — and so the check
    does not require importing ``vcr`` in this module.

    Walks ``__wrapped__`` to handle stacked decorators (e.g.
    ``@pytest.mark.parametrize`` on top of ``@notebooklm_vcr.use_cassette``).
    """
    func = getattr(item, "function", None)
    seen: set[int] = set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        wrapper = getattr(func, "_self_wrapper", None)
        if wrapper is not None:
            owner = getattr(wrapper, "__self__", None)
            if owner is not None and type(owner).__name__ == "CassetteContextDecorator":
                return True
        func = getattr(func, "__wrapped__", None)
    return False


def pytest_collection_modifyitems(config, items):
    """Auto-apply xfail to T8.A1-surfaced cassette-drift failures, then
    enforce the integration tier-VCR rule (T8.D11).

    **T8.A1 xfail layer.** Adding ``rpcids`` to the default VCR matcher
    surfaces cassettes whose recorded rpc-call order doesn't match live call
    order. Re-recording is phase-2 work (T8.B*); until then, mark these
    tests xfail so CI stays green and the phase-2 PRs that re-record each
    cassette can simply remove the entry from ``_T8_A1_XFAIL_NODEIDS``.

    **T8.D11 tier-enforcement layer.** After xfail-tagging, every collected
    test under ``tests/integration/`` MUST be VCR-tier: it must carry
    ``@pytest.mark.vcr``, be decorated with ``@notebooklm_vcr.use_cassette``,
    or explicitly opt out with ``@pytest.mark.allow_no_vcr`` (for mock-only
    or no-network tests that legitimately live under ``tests/integration/``
    — e.g. ``test_skill_packaging.py``, ``concurrency/test_*``). Violations
    raise ``pytest.UsageError`` so the test suite refuses to collect rather
    than silently letting a new mock test slip into the integration tier.
    """
    marker = pytest.mark.xfail(
        reason="T8.A1 matcher tightening surfaced cassette drift; phase-2 T8.B* re-records",
        strict=False,
    )
    for item in items:
        if item.nodeid in _T8_A1_XFAIL_NODEIDS:
            item.add_marker(marker)

    # T8.D11 — tier enforcement.
    violations: list[str] = []
    for item in items:
        nodeid = item.nodeid
        if not nodeid.startswith("tests/integration/"):
            continue
        if item.get_closest_marker("vcr") is not None:
            continue
        if item.get_closest_marker("allow_no_vcr") is not None:
            continue
        if _has_use_cassette_decorator(item):
            continue
        violations.append(nodeid)
    if violations:
        joined = "\n  ".join(violations)
        raise pytest.UsageError(
            "tests/integration/ tests must be VCR-tier. Add "
            "@pytest.mark.vcr, @notebooklm_vcr.use_cassette, or — for "
            "mock-only tests — @pytest.mark.allow_no_vcr. Violations:\n  "
            f"{joined}"
        )


# =============================================================================
# T8.D4 — Globalize keepalive-poke disable for VCR tests
# =============================================================================


@pytest.fixture(autouse=True)
def _disable_keepalive_poke_for_vcr(request, monkeypatch):
    """Auto-set ``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` for VCR tests.

    The layer-1 ``RotateCookies`` keepalive poke (documented escape hatch in
    CHANGELOG ``[0.4.1]`` Fixed) fires from inside ``_fetch_tokens_with_jar``
    and is not part of any cassette recorded before that poke was added.
    Letting it fire during VCR replay produces a cassette mismatch on
    ``POST accounts.google.com/RotateCookies``, which under the typed CLI
    error handler (P3.T2 / I14) surfaces as ``UNEXPECTED_ERROR`` (exit 2) —
    outside what most VCR tests accept. Disabling the poke aligns every
    replay with what the cassettes actually capture.

    A test is treated as VCR if ``request.node.get_closest_marker("vcr")``
    returns a marker. Every cassette-using test in this repo carries the
    ``vcr`` mark via either a module-level ``pytestmark = [pytest.mark.vcr,
    ...]`` or a per-test ``@pytest.mark.vcr`` decorator, so the marker check
    alone is sufficient — no stack inspection of
    ``@notebooklm_vcr.use_cassette`` is required. If a future test uses
    ``use_cassette`` without the marker, add ``@pytest.mark.vcr`` to it.

    Escape hatch: ``@pytest.mark.no_keepalive_disable`` opts a test out so
    it can capture or assert on real ``RotateCookies`` traffic (e.g. a
    future cassette that records the keepalive itself).

    Markers are read at SETUP TIME via ``get_closest_marker`` —
    ``request.applymarker()`` in the test body would be too late because the
    env var must be set before the client constructs its HTTP layer.
    """
    if request.node.get_closest_marker("no_keepalive_disable"):
        return
    if request.node.get_closest_marker("vcr") is None:
        return
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")


@pytest.fixture
def auth_tokens():
    """Create test authentication tokens for integration tests.

    Overrides the root-level fixture (single-cookie) with the full Tier 1
    cookie set so integration tests that exercise auth pre-flight validation
    have a realistic jar to work with.
    """
    return AuthTokens(
        cookies={
            "SID": "test_sid",
            "HSID": "test_hsid",
            "SSID": "test_ssid",
            "APISID": "test_apisid",
            "SAPISID": "test_sapisid",
        },
        csrf_token="test_csrf_token",
        session_id="test_session_id",
    )


# ``build_rpc_response`` and ``mock_list_notebooks_response`` are provided by
# the root ``tests/conftest.py`` and inherited here.
