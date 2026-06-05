"""Gate: every v0.8.0-breaking change is either runwayed or reason-exempted.

The v0.8.0 program (ADR-0019, umbrella **#1346**) lands a batch of breaking
public-API changes. ADR-0018 (deprecation strategy) says a breaking change
should ship a v0.7.0 *runway* — a ``DeprecationWarning`` or a stderr transition
notice — so callers get a signal *before* the break. But not every break can
warn at the value level (e.g. flipping a return type, or re-raising a refusal
that is currently swallowed): those are *deliberate clean breaks* in the
already-breaking 0.8.0, documented in the ADR + the upgrade guide instead.

The failure mode this gate exists to prevent is a **silent-and-unexplained**
break: a future v0.8.0 change that neither warns today nor records *why* it
cannot. "Enforce, don't document" — a documented transition that nothing checks
re-accretes the moment someone adds the next break without a runway.

This is a registry lint in the self-draining-allowlist idiom already used across
``tests/_guardrails/`` (``test_no_raw_positional_rpc_indexing.py``,
``test_module_size_ratchet.py``, ``tests/unit/test_public_api_contract.py``'s
``GET_OPTIONAL_EXEMPTIONS``). :data:`V080_BREAKING_CHANGES` declares every
tracked v0.8.0 break, each tagged with **exactly one** of:

* ``runway=<Runway>`` — a v0.7.0 signal that the gate *verifies actually exists*
  (the named warn helper is imported/called in the cited module, or the notice
  substring is present in the cited module). A claimed runway that isn't there
  fails the gate, so the table can't lie about coverage.
* ``exemption=<reason>`` — a reason-tagged justification that the break cannot
  or should not warn at the value level. This set is meant to **shrink** as
  runways are added; it must never silently grow.

The three breaks exempted today (#1290 bool-always-True returns, #1342
synchronous-refusal-suppression, #1362 update/rename fail-loud) are *value-level
behavioral changes with no clean warning point* — per ADR-0019 they are
deliberate clean breaks covered by the ADR + upgrade guide, not by a runtime
warning. They are exempted (not omitted) so the gate is green on ``main`` today
while still forcing a deliberate {runway, exemption} decision for any FUTURE
v0.8.0 break someone adds to the table.

Self-tests below prove the detector is not a no-op (a planted silent entry is
caught; a claimed-but-absent runway is caught) and that the table stays honest
(no entry has both/neither tag; every cited module exists; every exemption
carries a non-empty reason).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"


@dataclass(frozen=True)
class Runway:
    """A v0.7.0 deprecation signal whose *presence today* the gate can verify.

    Exactly one of ``symbol`` / ``notice`` is set:

    * ``symbol`` — a warn-helper / mixin name (e.g. ``warn_get_returns_none``)
      that must appear in :attr:`module`. This proves the runway is wired into
      the public surface, not merely defined in ``_deprecation.py``.
    * ``notice`` — a substring of a stderr transition notice (e.g. the
      ``generate mind-map`` ``--kind`` nudge) that must appear in
      :attr:`module`. Used for CLI notices that aren't routed through a named
      ``DeprecationWarning`` helper.

    ``module`` is POSIX-relative to ``src/notebooklm/``. ``description`` is the
    human-readable "how it warns today" note that the table column records.
    """

    module: str
    description: str
    symbol: str | None = None
    notice: str | None = None

    def __post_init__(self) -> None:
        provided = [v for v in (self.symbol, self.notice) if v is not None]
        if len(provided) != 1:
            raise ValueError(
                "Runway must set exactly one of {symbol, notice}; "
                f"got symbol={self.symbol!r}, notice={self.notice!r}"
            )

    @property
    def needle(self) -> str:
        """The literal string the gate searches for in :attr:`module`."""
        # Exactly one is non-None (enforced in __post_init__).
        return self.symbol if self.symbol is not None else self.notice  # type: ignore[return-value]

    @property
    def is_symbol(self) -> bool:
        """True for a ``symbol`` runway (verified against code, not comments)."""
        return self.symbol is not None


@dataclass(frozen=True)
class BreakingChange:
    """One tracked v0.8.0-breaking change, runwayed XOR reason-exempted.

    ``issue`` is the tracking issue number (e.g. ``1247``); ``summary`` names
    the break for failure messages. Exactly one of ``runway`` / ``exemption``
    must be set — :func:`_tag` asserts the XOR so no entry is silent-and-
    unexplained and none is over-tagged.
    """

    issue: int
    summary: str
    runway: Runway | None = None
    exemption: str | None = None


# Shared exemption reason for the value-level behavioral breaks that have no
# clean runtime warning point. Per ADR-0019 these are deliberate clean breaks in
# the already-breaking 0.8.0, covered by the ADR + the upgrade guide rather than
# by a v0.7.0 DeprecationWarning. Single-sourced so the three entries read
# identically and a reviewer sees one consistent justification.
_DELIBERATE_CLEAN_BREAK = (
    "documented: value-level behavioral change with no clean value-level warning "
    "(flipping a return type / re-raising a previously-swallowed refusal / adding "
    "a miss-detection raise cannot signal at the call site) — covered by ADR-0019 "
    "(docs/adr/0019-error-and-return-contract.md) + docs/deprecations.md upgrade "
    "guide. Deliberate clean break in the already-breaking 0.8.0."
)


# The registry. Anchored to the ADR-0019 contract (umbrella #1346) and the
# tracked v0.8.0 issues. Each entry carries EITHER a verified runway OR a
# reason-tagged exemption — never both, never neither.
#
# DO NOT add a silent entry. A new v0.8.0 break must either ship a v0.7.0 runway
# (and cite the module that wires it) or carry an explicit exemption reason.
# The exemption set is meant to SHRINK as runways are added.
V080_BREAKING_CHANGES: tuple[BreakingChange, ...] = (
    # ---- Exempted today (no clean value-level warning; shrink this set) -----
    BreakingChange(
        issue=1290,
        summary="bool-always-True returns -> None (refresh / delete_conversation / clear_cache)",
        exemption=_DELIBERATE_CLEAN_BREAK,
    ),
    BreakingChange(
        issue=1342,
        summary="synchronous generation refusal raises (drop status='failed' suppression)",
        exemption=_DELIBERATE_CLEAN_BREAK,
    ),
    BreakingChange(
        issue=1362,
        summary="mutate-existing fail-loud (notes.update + rename(return_object=False) raise on miss)",
        exemption=_DELIBERATE_CLEAN_BREAK,
    ),
    BreakingChange(
        issue=1344,
        summary="derived-read + lister drift-tightening: malformed payloads raise DecodingError",
        exemption=_DELIBERATE_CLEAN_BREAK,
    ),
)


# --- Pure detector (no I/O) ---------------------------------------------------
# Takes the table + a ``module -> source-text`` map so the public tests and the
# synthetic self-checks exercise the SAME logic. Keeping it I/O-free lets the
# self-checks feed crafted entries (silent / both-tagged / lying-runway) without
# touching the filesystem.


# Strips a trailing ``#`` comment from a line. Anchored after a non-``#`` run so
# a ``#`` inside an already-started comment doesn't matter, and a literal ``#``
# at column 0 (a full-line comment) is caught by the leading ``^[^#]*`` matching
# the empty prefix. This is a deliberately *coarse* filter: it does not parse
# string literals, so a ``#`` inside a string would truncate that line. That is
# fine here — a ``symbol`` needle (a bare identifier like ``deprecated_kwarg``)
# never legitimately lives inside a string literal, and the only thing we want
# to deny is a symbol that appears *solely* in a comment.
_COMMENT_RE = re.compile(r"#.*$", re.MULTILINE)


def _strip_comments(text: str) -> str:
    """Return ``text`` with ``#`` line-comments removed (coarse; see note above)."""
    return _COMMENT_RE.sub("", text)


def _tag(change: BreakingChange) -> str:
    """Return ``"runway"`` / ``"exemption"``, asserting exactly one is set.

    Raises :class:`ValueError` on a both-tagged or untagged entry — the
    silent-and-unexplained shape this gate forbids. Pure, so the self-check can
    drive it on crafted entries.
    """
    has_runway = change.runway is not None
    has_exemption = bool(change.exemption and change.exemption.strip())
    if has_runway and has_exemption:
        raise ValueError(
            f"#{change.issue} ({change.summary}) is BOTH runwayed and exempted; "
            "an entry carries exactly one of {runway, exemption}."
        )
    if not has_runway and not has_exemption:
        raise ValueError(
            f"#{change.issue} ({change.summary}) is silent-and-unexplained: it has "
            "neither a runway nor an exemption reason. Add a verified v0.7.0 runway "
            "or a reason-tagged exemption (ADR-0019 / #1346)."
        )
    return "runway" if has_runway else "exemption"


def _missing_runways(
    changes: tuple[BreakingChange, ...], sources: dict[str, str]
) -> dict[int, str]:
    """Map ``issue -> reason`` for every runwayed entry whose runway is absent.

    A runway is "present" when its :attr:`Runway.needle` appears in the cited
    module. For a ``symbol`` runway (a warn-helper / mixin identifier) the search
    is over a **comment-stripped** view of the source, so a symbol mentioned only
    in a comment or docstring-like ``#`` line does not falsely satisfy the runway
    — the signal must be in code. For a ``notice`` runway (a stderr transition
    string) the search is over the raw text, since the notice legitimately lives
    inside a string literal. An entry that cites a module missing from
    ``sources``, or whose needle is absent, is flagged — so the table cannot
    claim a runway that isn't wired in. Pure on its inputs.
    """
    missing: dict[int, str] = {}
    for change in changes:
        if change.runway is None:
            continue
        runway = change.runway
        text = sources.get(runway.module)
        if text is None:
            missing[change.issue] = f"cited module {runway.module!r} not found"
            continue
        haystack = _strip_comments(text) if runway.is_symbol else text
        if runway.needle not in haystack:
            where = "code (excluding comments)" if runway.is_symbol else runway.module
            missing[change.issue] = (
                f"runway needle {runway.needle!r} not found in {where!r}"
                if runway.is_symbol
                else f"runway needle {runway.needle!r} not found in {runway.module!r}"
            )
    return missing


# --- Filesystem helpers (I/O at the edge) -------------------------------------


def _cited_modules() -> dict[str, str]:
    """Read every module cited by a runway in the table -> ``{rel-path: text}``.

    Only modules actually referenced by a runway are read (the table is small),
    so the map is exactly the set the detector needs.
    """
    sources: dict[str, str] = {}
    for change in V080_BREAKING_CHANGES:
        if change.runway is None:
            continue
        rel = change.runway.module
        if rel in sources:
            continue
        path = SRC_ROOT / rel
        # Key absent <-> module missing (the detector flags it "not found");
        # key present (even an empty "") <-> module exists (the detector then
        # checks the needle). So only set the key when the file actually exists.
        if path.is_file():
            sources[rel] = path.read_text(encoding="utf-8")
    return sources


# --- Public gate tests --------------------------------------------------------


@pytest.mark.parametrize("change", V080_BREAKING_CHANGES, ids=lambda c: f"#{c.issue}")
def test_every_entry_is_runwayed_or_exempted(change: BreakingChange) -> None:
    """Every v0.8.0 break carries exactly one of {runway, exemption}.

    The core invariant: no entry may be silent-and-unexplained (no tag) or
    over-tagged (both). :func:`_tag` raises on either, so a new break added to
    the table without a deliberate {runway | exemption} decision fails here.
    """
    # Raises ValueError (surfaced as a test failure) on a both/neither entry.
    assert _tag(change) in {"runway", "exemption"}


def test_claimed_runways_actually_exist() -> None:
    """Every runwayed entry's signal is present in its cited module.

    Verifies the table cannot lie: if an entry claims ``warn_get_returns_none``
    runs in ``_sources.py`` (or a notice substring lives in ``generate.py``),
    that symbol/substring must actually be there. A runway that was removed or
    renamed without re-baselining the table fails here.
    """
    missing = _missing_runways(V080_BREAKING_CHANGES, _cited_modules())
    assert missing == {}, (
        "Claimed v0.7.0 runway(s) are not present in the cited module — the table "
        "claims a runway that isn't wired in (ADR-0018 / ADR-0019). Re-point the "
        "Runway to the module that actually emits the signal, or downgrade the "
        "entry to a reason-tagged exemption:\n"
        + "\n".join(f"  #{issue}: {why}" for issue, why in sorted(missing.items()))
    )


def test_exemptions_carry_a_nonempty_reason() -> None:
    """Every exempted entry has a non-empty, reason-tagged justification.

    Mirrors ``GET_OPTIONAL_EXEMPTIONS`` (``test_public_api_contract.py``): an
    exemption with a blank reason is a silent break in disguise. The reason must
    be present so every gap is visible in review and the set can be audited to
    shrink as runways are added.
    """
    blank = [
        f"#{c.issue} ({c.summary})"
        for c in V080_BREAKING_CHANGES
        if c.exemption is not None and not c.exemption.strip()
    ]
    assert blank == [], (
        "Exempted v0.8.0 break(s) carry an empty reason — a silent break in "
        "disguise. Add a justification (why it cannot warn at the value level) "
        "or give it a verified runway:\n" + "\n".join(f"  {b}" for b in blank)
    )


def test_cited_runway_modules_exist() -> None:
    """Every module a runway cites must exist under ``src/notebooklm/``.

    A rename/move that leaves a runway pointing at a vanished module would let
    :func:`test_claimed_runways_actually_exist` flag it as "not found", but this
    names the path-existence failure directly so the fix (re-point the module)
    is obvious.
    """
    missing = sorted(
        {
            c.runway.module
            for c in V080_BREAKING_CHANGES
            if c.runway is not None and not (SRC_ROOT / c.runway.module).is_file()
        }
    )
    assert missing == [], (
        "Runway(s) cite a module that no longer exists under src/notebooklm/ "
        "(renamed or moved). Re-point the Runway.module:\n" + "\n".join(f"  {m}" for m in missing)
    )


def test_issue_numbers_are_unique() -> None:
    """No two table entries share a tracking-issue number.

    A duplicate issue number means a break was double-listed (one row could go
    stale silently while the other passes). Each tracked break appears once.
    """
    seen: dict[int, int] = {}
    for change in V080_BREAKING_CHANGES:
        seen[change.issue] = seen.get(change.issue, 0) + 1
    dupes = sorted(issue for issue, n in seen.items() if n > 1)
    assert dupes == [], f"duplicate issue numbers in V080_BREAKING_CHANGES: {dupes}"


def test_silent_break_exemptions_match_baseline() -> None:
    """The value-level silent breaks are exactly the reason-tagged exemptions.

    Pins today's baseline: the value-level behavioral breaks that cannot ship a
    v0.7.0 warning runway are exempted (green on main), and the exemption set is
    *exactly* this list. If a future break is exempted, this test fails and forces
    a review of whether it truly cannot be runwayed (the set is meant to shrink,
    not grow). If one of these gains a runway, this also fails — a prompt to drop
    it from the exemption baseline.

    #1344 (derived-read / lister drift -> DecodingError) joins the original three
    (#1290 / #1342 / #1362): a "this future payload shape will be rejected" break
    has no value-level warning it could emit in v0.7.0, so it is a clean break.
    """
    # Match ``_tag``'s blank-reason handling: a whitespace-only exemption is "no
    # reason" (it would already fail ``test_every_entry_is_runwayed_or_exempted``),
    # so it must not be counted toward the baseline here either.
    exempted = sorted(c.issue for c in V080_BREAKING_CHANGES if c.exemption and c.exemption.strip())
    assert exempted == [1290, 1342, 1344, 1362], (
        "The v0.8.0 silent-break exemption set changed. It is meant to SHRINK as "
        "runways are added, never to grow silently. If you added a NEW exemption, "
        "confirm in review that the break genuinely cannot warn at the value level "
        "(ADR-0019) before updating this baseline; if you RUNWAYED one, drop it "
        f"here. Current exemptions: {exempted}"
    )


# --- Self-checks: prove the detector is not a no-op ---------------------------


def test_tag_rejects_silent_and_double_tagged_entries() -> None:
    """``_tag`` raises on the silent (no-tag) and both-tagged shapes.

    The core self-check: feeds crafted entries to the *real* ``_tag`` so we
    verify it BITES, not just that the live table happens to pass. A silent
    entry (the failure mode the gate exists to catch) and an over-tagged entry
    both raise ValueError.
    """
    silent = BreakingChange(issue=9999, summary="planted silent break")
    with pytest.raises(ValueError, match="silent-and-unexplained"):
        _tag(silent)

    both = BreakingChange(
        issue=9998,
        summary="planted double-tagged break",
        runway=Runway(module="_sources.py", description="x", symbol="warn_get_returns_none"),
        exemption="also exempted",
    )
    with pytest.raises(ValueError, match="BOTH runwayed and exempted"):
        _tag(both)

    # A whitespace-only exemption is NOT a real reason — it must read as silent.
    blank_reason = BreakingChange(issue=9997, summary="blank-reason break", exemption="   ")
    with pytest.raises(ValueError, match="silent-and-unexplained"):
        _tag(blank_reason)


def test_tag_accepts_a_valid_runway_and_a_valid_exemption() -> None:
    """``_tag`` returns the right label for each well-formed shape."""
    runwayed = BreakingChange(
        issue=1,
        summary="ok runway",
        runway=Runway(module="_sources.py", description="x", symbol="warn_get_returns_none"),
    )
    exempted = BreakingChange(issue=2, summary="ok exemption", exemption="documented reason")
    assert _tag(runwayed) == "runway"
    assert _tag(exempted) == "exemption"


def test_missing_runways_detects_absent_and_lying_runways() -> None:
    """``_missing_runways`` flags an absent module and a needle-not-present claim.

    Proves the runway-verification BITES: a runway that cites a module not in
    the source map is flagged, and a runway whose needle is absent from a
    present module is flagged — while a genuinely-present runway is not. This is
    what stops the table from claiming coverage it doesn't have.
    """
    sources = {
        "real.py": "def f():\n    warn_get_returns_none('source')\n",
        "empty.py": "# nothing relevant here\n",
    }
    present = BreakingChange(
        issue=10,
        summary="present runway",
        runway=Runway(module="real.py", description="x", symbol="warn_get_returns_none"),
    )
    absent_module = BreakingChange(
        issue=11,
        summary="runway cites missing module",
        runway=Runway(module="gone.py", description="x", symbol="warn_get_returns_none"),
    )
    lying = BreakingChange(
        issue=12,
        summary="runway needle not in module",
        runway=Runway(module="empty.py", description="x", symbol="warn_get_returns_none"),
    )
    # An exempted entry is never checked for a runway.
    exempted = BreakingChange(issue=13, summary="exempt", exemption="documented")

    result = _missing_runways((present, absent_module, lying, exempted), sources)
    assert set(result) == {11, 12}
    assert "not found" in result[11]
    assert "needle" in result[12]


def test_missing_runways_verifies_notice_substring() -> None:
    """A ``notice`` runway is verified by substring, not by symbol.

    The CLI ``--kind`` transition notice is a stderr string, not a named warn
    helper, so its runway is a substring match. Confirms a present notice passes
    and an absent one is flagged.
    """
    sources = {"cli.py": "warnings.append('... default switches to interactive in v0.8.0 ...')\n"}
    present = BreakingChange(
        issue=20,
        summary="notice present",
        runway=Runway(
            module="cli.py",
            description="x",
            notice="default switches to interactive in v0.8.0",
        ),
    )
    absent = BreakingChange(
        issue=21,
        summary="notice absent",
        runway=Runway(module="cli.py", description="x", notice="some other notice text"),
    )
    result = _missing_runways((present, absent), sources)
    assert set(result) == {21}


def test_symbol_runway_ignores_comment_only_mentions() -> None:
    """A ``symbol`` runway present only in a comment is flagged, not satisfied.

    Closes the substring-vs-code gap raised in review: the warn-helper signal
    must be in *code* (an import/call), so a symbol that appears solely in a
    ``#`` comment or docstring-like line does not falsely satisfy the runway. A
    real in-code call still passes.
    """
    comment_only = {"c.py": "# we used to call warn_get_returns_none here but removed it\nx = 1\n"}
    in_code = {"c.py": "def get():\n    warn_get_returns_none('source')  # warns on miss\n"}
    change = BreakingChange(
        issue=30,
        summary="symbol in comment only",
        runway=Runway(module="c.py", description="x", symbol="warn_get_returns_none"),
    )
    # Comment-only mention -> flagged (the code-only view has no match)...
    flagged = _missing_runways((change,), comment_only)
    assert set(flagged) == {30}
    assert "excluding comments" in flagged[30]
    # ...while a genuine in-code call passes even with a trailing comment.
    assert _missing_runways((change,), in_code) == {}


def test_strip_comments_removes_full_and_trailing_comments() -> None:
    """``_strip_comments`` drops full-line and trailing ``#`` comments.

    Unit-checks the coarse comment filter the symbol-runway verification relies
    on: a leading-``#`` full-line comment vanishes, a trailing comment is cut at
    the ``#``, and code before the ``#`` survives.
    """
    stripped = _strip_comments("# full line\ncode = 1  # trailing\nplain = 2\n")
    assert "full line" not in stripped
    assert "trailing" not in stripped
    assert "code = 1" in stripped
    assert "plain = 2" in stripped


def test_runway_rejects_zero_or_two_signals() -> None:
    """A ``Runway`` must set exactly one of {symbol, notice}.

    Guards the table's own constructor: a runway with neither (unverifiable) or
    both (ambiguous) signal is a malformed claim and must not be constructible.
    """
    with pytest.raises(ValueError, match="exactly one"):
        Runway(module="_sources.py", description="x")  # neither
    with pytest.raises(ValueError, match="exactly one"):
        Runway(module="_sources.py", description="x", symbol="a", notice="b")  # both


def test_gate_catches_a_planted_silent_break_in_the_table_shape() -> None:
    """A would-be NEW silent break added to a table fails the per-entry gate.

    Simulates the gate's real job end-to-end on the table shape: appending a
    bare ``BreakingChange`` (no runway, no exemption) to the registry makes the
    per-entry tag check raise. This is the regression the gate must always
    catch — restore by NOT adding such an entry to V080_BREAKING_CHANGES.
    """
    planted = (*V080_BREAKING_CHANGES, BreakingChange(issue=99999, summary="silent future break"))
    with pytest.raises(ValueError, match="silent-and-unexplained"):
        for change in planted:
            _tag(change)
