"""Guard: every cassette-backed RPC family is golden-decode-covered or exempt.

The VCR cassette matcher (``tests/vcr_config.py``) compares request **shape**,
never response leaves, so a decode regression that puts a wrong value in the
right slot replays green. The compensating control is the *golden decoded-row*
suite (``tests/integration/test_golden_decoded_vcr.py`` and
``test_golden_decoded_vcr_expansion.py``): for each recorded RPC family it pins
decoded dataclass field values, so positional-decode drift fails loudly.

This gate keeps that compensating control complete as the cassette corpus
grows. It enumerates every ``rpcids`` value recorded under ``tests/cassettes/``
(recursively — ``examples/`` fixtures are illustrative, not replayed) and
asserts each one is EITHER:

1. **Covered** — listed in :data:`GOLDEN_COVERAGE`, keyed by the
   :class:`~notebooklm.rpc.RPCMethod` constant and mapped to one or more test
   pointers ``(file, qualified test name)``. Each pointer is verified by AST:
   the function must exist at that exact ``Class::name`` location, and at
   least one cassette named in its ``use_cassette`` decorators must really
   record this rpcid — so a pointer can't silently rot into a comment, a
   same-named sibling test, or a test that replays an unrelated cassette, OR
2. **Exempt** — listed in :data:`GOLDEN_EXEMPT` with one of the sanctioned
   reasons, chosen by READING the client decode path: either the client
   discards the RPC's response outright, or the method's success contract is
   ``None`` so there is no decoded payload to pin.

Keying by ``RPCMethod`` (not by obfuscated string literals) keeps
``rpc/types.py`` the single source of truth: when Google rotates an ID and the
cassettes are re-recorded, this gate follows automatically. A cassette
recording an rpcid that no current ``RPCMethod`` knows fails loudly — that is
either a stale cassette or an un-modelled RPC, both worth a human look.

Granularity (a deliberate scoping decision): the gate is **family-level** —
one rpcid is satisfied by at least one golden pointer per entry. Where a
family has multiple recorded response *shapes* today (web vs drive freshness,
text vs url add, notes vs mind-map rows), each shape gets its own pointer in
:data:`GOLDEN_COVERAGE`, and all pointers are verified. But a *future* cassette
recording an already-covered rpcid with a brand-new shape does not, by itself,
force a new golden test — per-shape assertion depth is the golden modules' job
(and the review's), not this gate's. The gate guarantees no family is ever
fully un-pinned.

New cassettes start gated: recording a new RPC family without classifying it
here fails :func:`test_every_cassette_rpcid_is_classified` with instructions.

Modelled on the covered-or-exempt gate in
``tests/_guardrails/test_cli_vcr_coverage.py``.
"""

from __future__ import annotations

import ast
import re
from functools import cache
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
#
# Deliberately FULL-TEXT (not scoped to ``uri:`` lines): a hypothetical
# ``?rpcids=...`` URL inside a recorded response body would be counted too,
# but that failure mode is LOUD (an unknown/unclassified rpcid fails the
# gate and a human looks), whereas scoping to ``uri:`` lines could silently
# DROP a real request id if YAML ever folds a long URI across lines —
# turning the gate vacuous for that family. Loud-over-silent wins.
_RPCIDS_RE = re.compile(r"[?&]rpcids=([A-Za-z0-9]+)")

# Golden-covered families → one or more pointers ``(file relative to repo
# root, qualified test name)``. A qualified name is ``ClassName::test_name``
# for a method or a bare ``test_name`` for a module-level function. Every
# pointer names a test that pins at least one DECODED field value from a
# cassette replay of that RPC; multi-pointer entries pin each recorded
# response shape of the family. Most live in the two golden modules; three
# families are pinned where their only decoded contract already had
# exact-value assertions.
_GOLDEN_VCR = "tests/integration/test_golden_decoded_vcr.py"
_GOLDEN_EXPANSION = "tests/integration/test_golden_decoded_vcr_expansion.py"
_COMPREHENSIVE = "tests/integration/test_vcr_comprehensive.py"
_GAP_BACKFILL = "tests/integration/test_rpc_gap_backfill_vcr.py"

GoldenPointer = tuple[str, str]

GOLDEN_COVERAGE: dict[RPCMethod, tuple[GoldenPointer, ...]] = {
    # --- original high-risk four (issue #1494) ---
    RPCMethod.GET_LAST_CONVERSATION_ID: (
        (_GOLDEN_VCR, "TestChatGoldenDecoded::test_ask_decoded_golden"),
        (_GOLDEN_VCR, "TestChatGoldenDecoded::test_ask_with_references_decoded_golden"),
    ),
    RPCMethod.LIST_ARTIFACTS: (
        (_GOLDEN_VCR, "TestArtifactsListGoldenDecoded::test_list_decoded_golden"),
        (_GOLDEN_VCR, "TestArtifactsListGoldenDecoded::test_list_reports_decoded_golden"),
    ),
    RPCMethod.GET_NOTEBOOK: (
        # Source rows (sources_list.yaml) + the notebook row itself
        # (notebooks_get.yaml) — two distinct decoded views of rLM1Ne.
        (_GOLDEN_VCR, "TestSourcesGoldenDecoded::test_list_decoded_golden"),
        (_GOLDEN_EXPANSION, "TestNotebooksGoldenDecoded::test_get_decoded_golden"),
    ),
    RPCMethod.GET_SOURCE_GUIDE: (
        (_GOLDEN_VCR, "TestSourcesGoldenDecoded::test_get_guide_decoded_golden"),
    ),
    RPCMethod.GET_SOURCE: (
        (_GOLDEN_VCR, "TestSourcesGoldenDecoded::test_get_fulltext_decoded_golden"),
    ),
    # --- notebooks ---
    RPCMethod.LIST_NOTEBOOKS: (
        (_GOLDEN_EXPANSION, "TestNotebooksGoldenDecoded::test_list_decoded_golden"),
    ),
    RPCMethod.SUMMARIZE: (
        # Plain-summary and description+topics — the two decoded VfAZjd views.
        (_GOLDEN_EXPANSION, "TestNotebooksGoldenDecoded::test_get_summary_decoded_golden"),
        (_GOLDEN_EXPANSION, "TestNotebooksGoldenDecoded::test_get_description_decoded_golden"),
    ),
    RPCMethod.CREATE_NOTEBOOK: (
        (_GOLDEN_EXPANSION, "TestNotebooksGoldenDecoded::test_create_decoded_golden"),
    ),
    # --- sources ---
    RPCMethod.ADD_SOURCE: (
        # Text and URL adds return differently-shaped izAoDd rows.
        (_GOLDEN_EXPANSION, "TestSourceMutationsGoldenDecoded::test_add_text_decoded_golden"),
        (_GOLDEN_EXPANSION, "TestSourceMutationsGoldenDecoded::test_add_url_decoded_golden"),
    ),
    RPCMethod.ADD_SOURCE_FILE: (
        (_GOLDEN_EXPANSION, "TestSourceMutationsGoldenDecoded::test_add_file_decoded_golden"),
    ),
    RPCMethod.UPDATE_SOURCE: (
        (_GOLDEN_EXPANSION, "TestSourceMutationsGoldenDecoded::test_rename_decoded_golden"),
    ),
    # --- notes / mind maps ---
    RPCMethod.GET_NOTES_AND_MIND_MAPS: (
        # The empty-notes view and the mind-map-row view of the same payload.
        (_GOLDEN_EXPANSION, "TestNotesGoldenDecoded::test_list_decoded_golden"),
        (
            _GOLDEN_EXPANSION,
            "TestMindMapsGoldenDecoded::test_interactive_list_and_tree_decoded_golden",
        ),
    ),
    RPCMethod.CREATE_NOTE: (
        (_GOLDEN_EXPANSION, "TestNotesGoldenDecoded::test_create_decoded_golden"),
        # The mind-map chain's note_id pin is the decoded CREATE_NOTE id too.
        (_GOLDEN_EXPANSION, "TestMindMapsGoldenDecoded::test_generate_mind_map_decoded_golden"),
    ),
    RPCMethod.GET_INTERACTIVE_HTML: (
        (
            _GOLDEN_EXPANSION,
            "TestMindMapsGoldenDecoded::test_interactive_list_and_tree_decoded_golden",
        ),
    ),
    RPCMethod.GENERATE_MIND_MAP: (
        (_GOLDEN_EXPANSION, "TestMindMapsGoldenDecoded::test_generate_mind_map_decoded_golden"),
    ),
    # --- chat ---
    RPCMethod.GET_CONVERSATION_TURNS: (
        (_GOLDEN_EXPANSION, "TestChatHistoryGoldenDecoded::test_get_history_decoded_golden"),
    ),
    # --- labels ---
    RPCMethod.LIST_LABELS: (
        (_GOLDEN_EXPANSION, "TestLabelsGoldenDecoded::test_list_decoded_golden"),
    ),
    RPCMethod.CREATE_LABEL: (
        (_GOLDEN_EXPANSION, "TestLabelsGoldenDecoded::test_create_decoded_golden"),
    ),
    # --- sharing ---
    RPCMethod.GET_SHARE_STATUS: (
        (_GOLDEN_EXPANSION, "TestSharingGoldenDecoded::test_get_status_decoded_golden"),
    ),
    # --- research ---
    RPCMethod.START_FAST_RESEARCH: (
        (_GOLDEN_EXPANSION, "TestResearchGoldenDecoded::test_start_fast_decoded_golden"),
    ),
    RPCMethod.START_DEEP_RESEARCH: (
        (_GOLDEN_EXPANSION, "TestResearchGoldenDecoded::test_start_deep_decoded_golden"),
    ),
    RPCMethod.POLL_RESEARCH: (
        (_GOLDEN_EXPANSION, "TestResearchGoldenDecoded::test_poll_decoded_golden"),
    ),
    # IMPORT_RESEARCH's decoded contract (the imported id/title list) is pinned
    # exactly in the gap-backfill module that owns its cassette.
    RPCMethod.IMPORT_RESEARCH: ((_GAP_BACKFILL, "test_import_research_rpc_has_cassette_coverage"),),
    # --- settings ---
    RPCMethod.GET_USER_SETTINGS: (
        (_GOLDEN_EXPANSION, "TestSettingsGoldenDecoded::test_get_output_language_decoded_golden"),
    ),
    RPCMethod.SET_USER_SETTINGS: (
        (_GOLDEN_EXPANSION, "TestSettingsGoldenDecoded::test_set_output_language_decoded_golden"),
    ),
    RPCMethod.GET_USER_TIER: (
        (_GOLDEN_EXPANSION, "TestSettingsGoldenDecoded::test_get_account_tier_decoded_golden"),
    ),
    # --- artifacts ---
    RPCMethod.CREATE_ARTIFACT: (
        (_GOLDEN_EXPANSION, "TestArtifactsWriteGoldenDecoded::test_generate_report_decoded_golden"),
    ),
    RPCMethod.GET_SUGGESTED_REPORTS: (
        (_GOLDEN_EXPANSION, "TestArtifactsWriteGoldenDecoded::test_suggest_reports_decoded_golden"),
    ),
    RPCMethod.EXPORT_ARTIFACT: (
        (_GOLDEN_EXPANSION, "TestArtifactsWriteGoldenDecoded::test_export_report_decoded_golden"),
    ),
    RPCMethod.REVISE_SLIDE: (
        (_GOLDEN_EXPANSION, "TestArtifactsWriteGoldenDecoded::test_revise_slide_decoded_golden"),
    ),
    # RETRY_ARTIFACT's decoded contract (task_id echo + "in_progress") is pinned
    # exactly where its cassette is owned.
    RPCMethod.RETRY_ARTIFACT: ((_COMPREHENSIVE, "TestArtifactsGenerateAPI::test_retry_failed"),),
    # CHECK_SOURCE_FRESHNESS decodes to a single boolean; BOTH recorded shapes
    # ([] for web, [[null, true, [id]]] for drive) are pinned to ``is True``
    # where the cassettes are owned.
    RPCMethod.CHECK_SOURCE_FRESHNESS: (
        (_COMPREHENSIVE, "TestSourcesAdditionalAPI::test_check_freshness"),
        (_COMPREHENSIVE, "TestSourcesAdditionalAPI::test_check_freshness_drive"),
    ),
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
    """Real cassettes anywhere under ``tests/cassettes/`` (examples/ excluded).

    Recursive so nested replay corpora (e.g. ``gzip_coverage/``) are gated
    too; the ``examples/`` directory and ``example_*`` files are illustrative
    fixtures, never replayed (same filter as
    ``tests/integration/conftest.py``).
    """
    return sorted(
        f
        for f in CASSETTES_DIR.rglob("*.yaml")
        if "examples" not in f.relative_to(CASSETTES_DIR).parts
        and not f.name.startswith("example_")
    )


def _recorded_rpcids(text: str) -> set[str]:
    """Extract the set of recorded ``rpcids`` query values from cassette text."""
    return set(_RPCIDS_RE.findall(text))


@cache
def _corpus_rpcids_by_cassette() -> dict[str, frozenset[str]]:
    """Map each cassette base name -> the rpcids it records (cached: ~40 MB read).

    Keyed by base name because that is what ``use_cassette`` decorators carry.
    Should two cassettes in different subdirectories ever share a base name,
    their rpcid sets are UNIONED (not last-wins), so a pointer can never be
    invalidated — or falsely satisfied for a *different* family — by a
    directory-level name collision.
    """
    corpus: dict[str, set[str]] = {}
    for path in _cassette_files():
        corpus.setdefault(path.name, set()).update(
            _recorded_rpcids(path.read_text(encoding="utf-8"))
        )
    return {name: frozenset(rpcids) for name, rpcids in corpus.items()}


def _corpus_rpcids() -> dict[str, set[str]]:
    """Map each recorded rpcid -> the cassette file names that record it."""
    corpus: dict[str, set[str]] = {}
    for name, rpcids in _corpus_rpcids_by_cassette().items():
        for rpcid in rpcids:
            corpus.setdefault(rpcid, set()).add(name)
    return corpus


def _decorator_cassettes(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Map qualified test name -> ``*.yaml`` literals in its decorator list.

    Qualified names are ``ClassName::method`` for class-level tests and the
    bare function name for module-level tests — mirroring how pytest node ids
    disambiguate the same-named methods that exist across golden classes. Only
    *decorator* string literals count (``@notebooklm_vcr.use_cassette("x.yaml",
    ...)``); a cassette name mentioned in the body or a comment does not.
    Pure on its input so the self-test can exercise it without touching disk.
    """

    def yaml_literals(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
        return frozenset(
            node.value
            for decorator in fn.decorator_list
            for node in ast.walk(decorator)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(".yaml")
        )

    out: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}::{sub.name}"] = yaml_literals(sub)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = yaml_literals(node)
    return out


@cache
def _file_decorator_cassettes(rel: str) -> dict[str, frozenset[str]] | None:
    """``_decorator_cassettes`` for a repo-relative file, or None if missing."""
    path = REPO_ROOT / rel
    if not path.is_file():
        return None
    return _decorator_cassettes(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))


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


def test_covered_pointers_are_real_tests_replaying_the_right_cassettes() -> None:
    """Every ``GOLDEN_COVERAGE`` pointer names a real test that replays this family.

    Three failure modes are rejected per pointer, so a mapping can't silently
    claim coverage that no longer exists:

    * the file is missing, or no test exists at the exact qualified
      ``Class::name`` location (a same-named test in ANOTHER class does not
      satisfy the pointer — names repeat across golden classes);
    * the test carries no ``use_cassette("*.yaml")`` decorator (it replays
      nothing);
    * none of its decorator cassettes actually records this entry's rpcid
      (the test replays an unrelated family).
    """
    broken: dict[str, list[str]] = {}
    by_cassette = _corpus_rpcids_by_cassette()
    for method, pointers in GOLDEN_COVERAGE.items():
        problems: list[str] = []
        if not pointers:
            problems.append("entry has no pointers")
        for rel, qualname in pointers:
            tests = _file_decorator_cassettes(rel)
            if tests is None:
                problems.append(f"missing file {rel}")
                continue
            if qualname not in tests:
                problems.append(f"no test at {rel}::{qualname}")
                continue
            cassettes = tests[qualname]
            if not cassettes:
                problems.append(f"{rel}::{qualname} has no use_cassette decorator")
                continue
            # Normalize to base names: a decorator may carry a subdirectory
            # path (e.g. ``"gzip_coverage/foo.yaml"``) while the corpus map is
            # keyed by base name.
            if not any(
                method.value in by_cassette.get(Path(name).name, frozenset()) for name in cassettes
            ):
                problems.append(
                    f"{rel}::{qualname} replays {sorted(cassettes)}, none of which "
                    f"records rpcid {method.value!r}"
                )
        if problems:
            broken[method.name] = problems
    assert broken == {}, (
        "GOLDEN_COVERAGE pointer(s) do not hold up — each pointer must name an "
        "existing test (exact Class::name) whose use_cassette decorator replays "
        "a cassette recording that family's rpcid. Fix or remove:\n"
        + "\n".join(f"  {name}: {problems}" for name, problems in sorted(broken.items()))
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
    # Full-text scan is INTENTIONAL (see the ``_RPCIDS_RE`` comment): a
    # ``?rpcids=`` URL inside a recorded response body IS extracted, because
    # that false positive fails the gate loudly, whereas scoping to ``uri:``
    # lines could silently drop a folded request URI and turn the gate
    # vacuous for that family.
    body_url = "    string: 'see https://x.test/page?rpcids=Zz9fake for details'"
    assert _recorded_rpcids(body_url) == {"Zz9fake"}


def test_decorator_cassette_collector_self_test() -> None:
    """The AST collector maps qualified names to decorator cassettes only.

    Pure-input self-test: class methods get ``Class::name`` keys, module
    functions get bare keys, multiple/parametrized decorator literals are all
    collected, and a cassette mentioned only in a body string or comment does
    NOT count — so the pointer check can't be satisfied by prose.
    """
    src = "\n".join(
        [
            "import pytest",
            "class TestAlpha:",
            "    @pytest.mark.vcr",
            "    @notebooklm_vcr.use_cassette('alpha.yaml', match_on=['freq'])",
            "    async def test_one(self):",
            "        pass",
            "    async def test_bare(self):",
            "        x = 'body_mention.yaml'  # not a decorator",
            "@notebooklm_vcr.use_cassette('gamma.yaml')",
            "@notebooklm_vcr.use_cassette('delta.yaml')",
            "def test_module_level():",
            "    pass",
        ]
    )
    collected = _decorator_cassettes(ast.parse(src))
    assert collected == {
        "TestAlpha::test_one": frozenset({"alpha.yaml"}),
        "TestAlpha::test_bare": frozenset(),
        "test_module_level": frozenset({"gamma.yaml", "delta.yaml"}),
    }
