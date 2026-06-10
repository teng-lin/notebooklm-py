"""Guard: every cassette-backed RPC family is golden-decode-covered or exempt.

The VCR cassette matcher (``tests/vcr_config.py``) compares request **shape**,
never response leaves, so a decode regression that puts a wrong value in the
right slot replays green. The compensating control is the *golden decoded-row*
suite (``tests/integration/test_golden_decoded_vcr.py`` and
``test_golden_decoded_vcr_expansion.py``): for each recorded RPC family it pins
decoded dataclass field values, so positional-decode drift fails loudly.

This gate keeps that compensating control complete as the cassette corpus
grows. It enumerates every ``rpcids`` value recorded in ``tests/cassettes/``
(top level — ``examples/`` fixtures are illustrative, not replayed) and asserts
each one is EITHER:

1. **Covered** — listed in :data:`GOLDEN_COVERAGE`, keyed by the
   :class:`~notebooklm.rpc.RPCMethod` constant and mapped to the test (file +
   test name) that pins at least one decoded-field value produced from a
   cassette replay of that RPC. The file must exist and contain the named
   test, OR
2. **Exempt** — listed in :data:`GOLDEN_EXEMPT` with one of the sanctioned
   reasons, chosen by READING the client decode path: either the client
   discards the RPC's response outright, or the method's success contract is
   ``None`` so there is no decoded payload to pin.

Keying by ``RPCMethod`` (not by obfuscated string literals) keeps
``rpc/types.py`` the single source of truth: when Google rotates an ID and the
cassettes are re-recorded, this gate follows automatically. A cassette
recording an rpcid that no current ``RPCMethod`` knows fails loudly — that is
either a stale cassette or an un-modelled RPC, both worth a human look.

New cassettes start gated: recording a new RPC family without classifying it
here fails :func:`test_every_cassette_rpcid_is_classified` with instructions.

Modelled on the covered-or-exempt gate in
``tests/_guardrails/test_cli_vcr_coverage.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from notebooklm.rpc import RPCMethod

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTES_DIR = REPO_ROOT / "tests" / "cassettes"

# Recorded request URIs carry the RPC family as a query param:
# ``.../batchexecute?rpcids=<id>&...``. The client sends exactly one rpcid per
# POST, and the ids are strictly alphanumeric (see ``rpc/types.py``), so a
# regex over the raw cassette text is exact — and far cheaper than YAML-parsing
# 40+ MB of recordings.
_RPCIDS_RE = re.compile(r"[?&]rpcids=([A-Za-z0-9]+)")

# Golden-covered families → (test file relative to repo root, test name).
# The named test pins at least one DECODED field value for a cassette replay of
# that RPC. Most live in the two golden modules; three are pinned where the
# family's only decoded contract already had exact-value assertions.
_GOLDEN_VCR = "tests/integration/test_golden_decoded_vcr.py"
_GOLDEN_EXPANSION = "tests/integration/test_golden_decoded_vcr_expansion.py"
_COMPREHENSIVE = "tests/integration/test_vcr_comprehensive.py"
_GAP_BACKFILL = "tests/integration/test_rpc_gap_backfill_vcr.py"

GOLDEN_COVERAGE: dict[RPCMethod, tuple[str, str]] = {
    # --- original high-risk four (issue #1494) ---
    RPCMethod.GET_LAST_CONVERSATION_ID: (_GOLDEN_VCR, "test_ask_decoded_golden"),
    RPCMethod.LIST_ARTIFACTS: (_GOLDEN_VCR, "test_list_decoded_golden"),
    RPCMethod.GET_NOTEBOOK: (_GOLDEN_VCR, "test_list_decoded_golden"),
    RPCMethod.GET_SOURCE_GUIDE: (_GOLDEN_VCR, "test_get_guide_decoded_golden"),
    RPCMethod.GET_SOURCE: (_GOLDEN_VCR, "test_get_fulltext_decoded_golden"),
    # --- notebooks ---
    RPCMethod.LIST_NOTEBOOKS: (_GOLDEN_EXPANSION, "test_list_decoded_golden"),
    RPCMethod.SUMMARIZE: (_GOLDEN_EXPANSION, "test_get_summary_decoded_golden"),
    RPCMethod.CREATE_NOTEBOOK: (_GOLDEN_EXPANSION, "test_create_decoded_golden"),
    # --- sources ---
    RPCMethod.ADD_SOURCE: (_GOLDEN_EXPANSION, "test_add_url_decoded_golden"),
    RPCMethod.ADD_SOURCE_FILE: (_GOLDEN_EXPANSION, "test_add_file_decoded_golden"),
    RPCMethod.UPDATE_SOURCE: (_GOLDEN_EXPANSION, "test_rename_decoded_golden"),
    # --- notes / mind maps ---
    RPCMethod.GET_NOTES_AND_MIND_MAPS: (
        _GOLDEN_EXPANSION,
        "test_interactive_list_and_tree_decoded_golden",
    ),
    RPCMethod.CREATE_NOTE: (_GOLDEN_EXPANSION, "test_create_decoded_golden"),
    RPCMethod.GET_INTERACTIVE_HTML: (
        _GOLDEN_EXPANSION,
        "test_interactive_list_and_tree_decoded_golden",
    ),
    RPCMethod.GENERATE_MIND_MAP: (_GOLDEN_EXPANSION, "test_generate_mind_map_decoded_golden"),
    # --- chat ---
    RPCMethod.GET_CONVERSATION_TURNS: (_GOLDEN_EXPANSION, "test_get_history_decoded_golden"),
    # --- labels ---
    RPCMethod.LIST_LABELS: (_GOLDEN_EXPANSION, "test_list_decoded_golden"),
    RPCMethod.CREATE_LABEL: (_GOLDEN_EXPANSION, "test_create_decoded_golden"),
    # --- sharing ---
    RPCMethod.GET_SHARE_STATUS: (_GOLDEN_EXPANSION, "test_get_status_decoded_golden"),
    # --- research ---
    RPCMethod.START_FAST_RESEARCH: (_GOLDEN_EXPANSION, "test_start_fast_decoded_golden"),
    RPCMethod.START_DEEP_RESEARCH: (_GOLDEN_EXPANSION, "test_start_deep_decoded_golden"),
    RPCMethod.POLL_RESEARCH: (_GOLDEN_EXPANSION, "test_poll_decoded_golden"),
    # IMPORT_RESEARCH's decoded contract (the imported id/title list) is pinned
    # exactly in the gap-backfill module that owns its cassette.
    RPCMethod.IMPORT_RESEARCH: (_GAP_BACKFILL, "test_import_research_rpc_has_cassette_coverage"),
    # --- settings ---
    RPCMethod.GET_USER_SETTINGS: (_GOLDEN_EXPANSION, "test_get_output_language_decoded_golden"),
    RPCMethod.SET_USER_SETTINGS: (_GOLDEN_EXPANSION, "test_set_output_language_decoded_golden"),
    RPCMethod.GET_USER_TIER: (_GOLDEN_EXPANSION, "test_get_account_tier_decoded_golden"),
    # --- artifacts ---
    RPCMethod.CREATE_ARTIFACT: (_GOLDEN_EXPANSION, "test_generate_report_decoded_golden"),
    RPCMethod.GET_SUGGESTED_REPORTS: (_GOLDEN_EXPANSION, "test_suggest_reports_decoded_golden"),
    RPCMethod.EXPORT_ARTIFACT: (_GOLDEN_EXPANSION, "test_export_report_decoded_golden"),
    RPCMethod.REVISE_SLIDE: (_GOLDEN_EXPANSION, "test_revise_slide_decoded_golden"),
    # RETRY_ARTIFACT's decoded contract (task_id echo + "in_progress") is pinned
    # exactly where its cassette is owned.
    RPCMethod.RETRY_ARTIFACT: (_COMPREHENSIVE, "test_retry_failed"),
    # CHECK_SOURCE_FRESHNESS decodes to a single boolean; both recorded shapes
    # ([] and [[null, true, [id]]]) are pinned to ``is True`` where the
    # cassettes are owned.
    RPCMethod.CHECK_SOURCE_FRESHNESS: (_COMPREHENSIVE, "test_check_freshness"),
}

# Sanctioned exemption reasons (named constants so a typo can't fork them).
_REASON_NONE_CONTRACT = (
    "success contract returns None; the response carries no decodable row to pin"
)
_REASON_RESPONSE_DISCARDED = (
    "client discards this RPC's response; any returned object is decoded from a "
    "separate (golden-covered) read RPC"
)

# Families with a cassette but nothing decodable to pin → reason. Verified by
# reading each client decode path (see the per-entry notes).
GOLDEN_EXEMPT: dict[RPCMethod, str] = {
    # Deletes / fire-and-forget writes: ``None`` on success by contract.
    RPCMethod.DELETE_NOTEBOOK: _REASON_NONE_CONTRACT,
    RPCMethod.DELETE_SOURCE: _REASON_NONE_CONTRACT,
    RPCMethod.DELETE_ARTIFACT: _REASON_NONE_CONTRACT,
    RPCMethod.DELETE_NOTE: _REASON_NONE_CONTRACT,
    RPCMethod.DELETE_LABEL: _REASON_NONE_CONTRACT,
    RPCMethod.DELETE_CONVERSATION: _REASON_NONE_CONTRACT,
    RPCMethod.REMOVE_RECENTLY_VIEWED: _REASON_NONE_CONTRACT,
    # ``sources.refresh`` returns None on success (v0.8.0, #1290).
    RPCMethod.REFRESH_SOURCE: _REASON_NONE_CONTRACT,
    # ``notes.update`` returns None (the UPDATE_NOTE echo is not decoded).
    RPCMethod.UPDATE_NOTE: _REASON_NONE_CONTRACT,
    # Rename/share/update writes whose returned object is re-fetched through a
    # covered read RPC (GET_NOTEBOOK / LIST_ARTIFACTS / LIST_LABELS /
    # GET_SHARE_STATUS), so the write response itself is never decoded.
    RPCMethod.RENAME_NOTEBOOK: _REASON_RESPONSE_DISCARDED,
    RPCMethod.RENAME_ARTIFACT: _REASON_RESPONSE_DISCARDED,
    RPCMethod.UPDATE_LABEL: _REASON_RESPONSE_DISCARDED,
    RPCMethod.SHARE_NOTEBOOK: _REASON_RESPONSE_DISCARDED,
    # Legacy ``notebooks.share`` (SHARE_ARTIFACT): the return dict is built
    # entirely from the caller's inputs; the response is discarded.
    RPCMethod.SHARE_ARTIFACT: _REASON_RESPONSE_DISCARDED,
}

_VALID_REASONS = frozenset({_REASON_NONE_CONTRACT, _REASON_RESPONSE_DISCARDED})


def _cassette_files() -> list[Path]:
    """Real cassettes: top-level ``tests/cassettes/*.yaml`` (examples/ excluded)."""
    return sorted(f for f in CASSETTES_DIR.glob("*.yaml") if not f.name.startswith("example_"))


def _recorded_rpcids(text: str) -> set[str]:
    """Extract the set of recorded ``rpcids`` query values from cassette text."""
    return set(_RPCIDS_RE.findall(text))


def _corpus_rpcids() -> dict[str, set[str]]:
    """Map each recorded rpcid -> the cassette file names that record it."""
    corpus: dict[str, set[str]] = {}
    for path in _cassette_files():
        for rpcid in _recorded_rpcids(path.read_text(encoding="utf-8")):
            corpus.setdefault(rpcid, set()).add(path.name)
    return corpus


def test_every_cassette_rpcid_is_classified() -> None:
    """Every rpcid recorded in ``tests/cassettes/`` is golden-covered or exempt.

    A new cassette that records an unclassified RPC family is a fresh blind
    spot for the shape-only matcher: add a golden decoded-row test (and map it
    in ``GOLDEN_COVERAGE``), or — only when the client genuinely decodes
    nothing from the response — add a ``GOLDEN_EXEMPT`` entry with one of the
    sanctioned reasons.
    """
    known_values = {method.value: method for method in RPCMethod}
    classified_values = {m.value for m in GOLDEN_COVERAGE} | {m.value for m in GOLDEN_EXEMPT}
    corpus = _corpus_rpcids()

    unknown = {rpcid: sorted(files) for rpcid, files in corpus.items() if rpcid not in known_values}
    assert unknown == {}, (
        "Cassette(s) record rpcid(s) that no current RPCMethod constant knows — "
        "either Google rotated an ID (update rpc/types.py and re-record) or a "
        f"stale cassette slipped in: {unknown}"
    )

    unclassified = {
        rpcid: sorted(files) for rpcid, files in corpus.items() if rpcid not in classified_values
    }
    assert unclassified == {}, (
        "Cassette-recorded RPC familie(s) have no golden decoded-row coverage and "
        "no exemption (golden-decode coverage gate). Add a golden test pinning a "
        "decoded field value (map it in GOLDEN_COVERAGE) or a reasoned "
        "GOLDEN_EXEMPT entry:\n"
        + "\n".join(
            f"  {RPCMethod(rpcid).name} ({rpcid}): recorded in {files}"
            for rpcid, files in sorted(unclassified.items())
        )
    )


def test_covered_entries_point_at_existing_tests() -> None:
    """Every ``GOLDEN_COVERAGE`` entry maps to a real file containing the named test.

    A dangling mapping (renamed test, deleted file) would silently claim
    coverage that no longer exists.
    """
    broken: dict[str, str] = {}
    for method, (rel, test_name) in GOLDEN_COVERAGE.items():
        path = REPO_ROOT / rel
        if not path.is_file():
            broken[method.name] = f"missing file {rel}"
        elif f"def {test_name}(" not in path.read_text(encoding="utf-8"):
            broken[method.name] = f"no test named {test_name!r} in {rel}"
    assert broken == {}, (
        f"GOLDEN_COVERAGE entries point at tests that do not exist — fix or remove them: {broken}"
    )


def test_covered_and_exempt_sets_are_disjoint() -> None:
    """No RPC family may be both covered and exempt."""
    overlap = sorted(m.name for m in set(GOLDEN_COVERAGE) & set(GOLDEN_EXEMPT))
    assert overlap == [], (
        f"RPC familie(s) appear in BOTH GOLDEN_COVERAGE and GOLDEN_EXEMPT: {overlap}. "
        "A covered family must not also be exempt."
    )


def test_every_exemption_has_a_sanctioned_reason() -> None:
    """Each ``GOLDEN_EXEMPT`` reason must be one of the sanctioned constants.

    Forces every exemption into an audited bucket so a free-text reason can't
    smuggle in an un-triaged blind spot.
    """
    bad = {m.name: r for m, r in GOLDEN_EXEMPT.items() if r not in _VALID_REASONS}
    assert bad == {}, (
        "GOLDEN_EXEMPT entries with an unrecognised reason — use one of the "
        f"sanctioned constants: {bad}"
    )


def test_no_stale_classifications() -> None:
    """Every classified family must still be recorded by at least one cassette.

    A classification whose cassettes were all deleted is dead weight that would
    mask a future re-recording under the same id arriving unreviewed.
    """
    recorded = set(_corpus_rpcids())
    stale = sorted(
        m.name for m in (set(GOLDEN_COVERAGE) | set(GOLDEN_EXEMPT)) if m.value not in recorded
    )
    assert stale == [], (
        "Classified RPC familie(s) are no longer recorded by any cassette — "
        f"remove the stale entries: {stale}"
    )


def test_rpcids_extractor_self_test() -> None:
    """The rpcid extractor finds query-param ids and ignores look-alikes.

    Pure-input self-test (no filesystem) so a regex regression in
    :data:`_RPCIDS_RE` cannot silently empty the corpus and turn the gate
    vacuous.
    """
    sample = "\n".join(
        [
            "interactions:",
            "- request:",
            "    uri: https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?rpcids=wXbhsf&source-path=%2F&f.sid=1",
            "- request:",
            "    uri: https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?bl=boq&rpcids=gArtLc&rt=c",
            "    body: 'f.req=%5B%5B%22rpcids%22%5D%5D'",  # body mention, not a query param
            "- request:",
            "    uri: https://accounts.google.com/RotateCookies",  # no rpcids at all
        ]
    )
    assert _recorded_rpcids(sample) == {"wXbhsf", "gArtLc"}
    assert _recorded_rpcids("no ids here") == set()
