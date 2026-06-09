"""Guard: no raw positional indexing of RPC payloads in feature code.

Google's ``batchexecute`` responses are positional lists (the project's #1
standing risk -- the shape can move without notice). The sanctioned places to
decode those positional structures are:

* ``src/notebooklm/rpc/`` -- the RPC protocol layer (encoder/decoder/safe_index),
  the home of ``safe_index`` itself; and
* ``src/notebooklm/_row_adapters/`` -- the typed row views (``ArtifactRow`` /
  ``NoteRow`` / ``SourceRow`` / the chat rows) that centralise position
  knowledge behind named properties.

Everywhere else, walking a decoded payload with hand-rolled integer-literal
subscripts re-scatters the position knowledge the adapters exist to contain,
and -- per **ADR-0011** -- routinely *swallows* shape drift to an empty/wrong
value behind ``try/except (IndexError, TypeError)`` instead of raising
``UnknownRPCMethodError`` via ``safe_index``.

This module runs **two** AST gates:

1. **Chained descent (issue #1377).** A ``Subscript`` indexed by an integer
   literal whose *own value* is another integer-literal ``Subscript`` -- i.e. a
   two-or-more-deep positional descent like ``x[i][j]`` (``first[4][3]``,
   ``result[0][2][4]``, ``cite[0][0]``). This is the most fragile "deep descent
   into an RPC payload" shape, and its :data:`ALLOWLIST` is **empty** -- the
   #1377 burndown migrated every chained offender, so the chained gate
   re-protects the whole feature tree with no exceptions.

2. **Single-level descent (issue #1491).** A single ``Subscript`` indexed by an
   integer literal (``x[i]``). On its own this is too common and too benign --
   ``args[0]``, ``parts[-1]`` -- to forbid outright, but it is *also* exactly how
   un-named row-position knowledge of an RPC payload leaks past the chained
   gate (the chat wire parser carried dozens of ``first[4]`` / ``cite[1]`` /
   ``passage_data[0]`` reads that the chained gate never saw). So the
   single-level gate works as a **ratchet**: the 47 feature files that already
   open-code single-level integer subscripts are *baselined* into
   :data:`SINGLE_LEVEL_ALLOWLIST` (so the gate is green on ``main`` today), but
   a *new* single-level integer subscript in a file that is NOT on that list
   fails the gate. New code therefore decodes through a row adapter / a named
   local rather than re-scattering raw positions. The burndown that drains
   :data:`SINGLE_LEVEL_ALLOWLIST` (migrating each file behind ``_row_adapters/``
   + ``safe_index`` or binding the guarded inner list to a named local) is
   tracked as a follow-up to #1491.

A string/slice subscript (``d["k"]``, ``s[1:]``) is ignored by both gates.

Both allowlists are self-draining: :func:`test_no_stale_allowlist_entries` and
:func:`test_no_stale_single_level_allowlist_entries` fail if an allowlisted file
no longer contains the offending shape, so once a file is migrated it must be
removed from its list (the gate then re-protects it).
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"

# Top-level packages under ``src/notebooklm`` that are *allowed* to decode raw
# positional RPC payloads: the RPC protocol layer and the typed row adapters.
SANCTIONED_PACKAGES = frozenset({"rpc", "_row_adapters"})

# Baseline of feature files that open-code chained positional descent into RPC
# payloads (issue #1377). The burndown (#1389) migrated every baselined file
# behind ``_row_adapters/`` + ``safe_index`` (or bound the already-guarded inner
# list to a named local so each leaf read is a single-level index), so the list
# is now EMPTY and the gate re-protects the whole feature tree.
#
# DO NOT add new entries to grow the debt -- a new offender means new code that
# should decode through ``safe_index`` / a row adapter instead.
ALLOWLIST: frozenset[str] = frozenset()

# Baseline of feature files that open-code a *single-level* integer-literal
# subscript (``x[i]``) of a decoded RPC payload (issue #1491). Many of these
# reads are benign (``params[2]``, ``args[0]``) and many are genuine but
# already-guarded inner reads -- the single-level gate is a RATCHET, not an
# immediate ban: these files are grandfathered so the gate is green on ``main``,
# but a single-level subscript in any file NOT on this list fails the gate.
#
# The chat wire parser (``_chat/wire.py``) and the ``suggest_reports`` row decode
# in ``_artifacts.py`` were the largest un-adapted surfaces; ``_chat/wire.py`` is
# fully migrated behind ``_row_adapters/chat.py`` and is DELIBERATELY ABSENT from
# this list so the gate re-protects it. ``_artifacts.py`` keeps its remaining
# envelope-unwrap / request-param reads (only its ``suggest_reports`` row decode
# moved behind ``ReportSuggestionRow``), so it stays listed for now.
#
# DO NOT add new entries to grow the debt. The burndown (drain this list by
# migrating each file behind ``_row_adapters/`` + ``safe_index``, or binding the
# already-guarded inner list to a named local) is a follow-up to #1491.
SINGLE_LEVEL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "_app/artifacts.py",
        "_app/download.py",
        "_app/generate_retry.py",
        "_app/labels.py",
        "_app/notes.py",
        "_app/resolve.py",
        "_app/skill.py",
        "_app/source_clean.py",
        "_app/source_mutations.py",
        "_artifact/downloads.py",
        "_artifact/formatters.py",
        "_artifact/listing.py",
        "_artifact/polling.py",
        "_artifacts.py",
        "_auth/cookies.py",
        "_auth/refresh.py",
        "_chat/api.py",
        "_chat/notes.py",
        "_labels.py",
        "_mind_maps_api.py",
        "_note_service.py",
        "_notebooks.py",
        "_notes.py",
        "_research.py",
        "_research_task_parser.py",
        "_source/add.py",
        "_source/content.py",
        "_source/listing.py",
        "_source/upload.py",
        "_types/artifacts.py",
        "_types/notebooks.py",
        "_types/sharing.py",
        "_types/sources.py",
        "_version_check.py",
        "cli/_chromium_profiles.py",
        "cli/_firefox_containers.py",
        "cli/agent_templates.py",
        "cli/artifact_cmd.py",
        "cli/error_handler.py",
        "cli/resolve.py",
        "cli/services/login/browser_accounts.py",
        "cli/services/login/chromium_accounts.py",
        "cli/services/login/cookie_writes.py",
        "cli/services/login/profile_targets.py",
        "cli/services/playwright_login.py",
        "cli/services/playwright_redaction.py",
        "utils.py",
    }
)


def _is_int_literal(node: ast.expr) -> bool:
    """True for an integer-literal index, positive or negative.

    Matches a bare ``ast.Constant`` int (``a[3]``), a negated literal
    ``ast.UnaryOp(USub, Constant(int))`` (``a[-1]``), and an explicit unary-plus
    literal ``ast.UnaryOp(UAdd, Constant(int))`` (``a[+1]``) -- a negative or
    explicitly-positive index is just as positional as a bare one, so the gate
    must not be sidestepped by ``payload[4][-1]`` or ``payload[+1][0]``. ``bool``
    subclasses ``int`` in Python; ``True``/``False`` indices are excluded so
    ``flags[True][False]`` is not treated as positional.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and node.value is not True
        and node.value is not False
    )


def _chained_positional_offenders(tree: ast.AST) -> list[int]:
    """Return sorted line numbers of chained integer-literal subscripts.

    A site is ``outer[j]`` where the index ``j`` is an integer literal *and*
    ``outer`` is itself ``inner[i]`` with an integer-literal index ``i`` -- the
    two-deep positional descent ``inner[i][j]``. Pure on its input so a planted
    fixture can exercise it without touching the filesystem.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Subscript) and _is_int_literal(node.slice)):
            continue
        inner = node.value
        if isinstance(inner, ast.Subscript) and _is_int_literal(inner.slice):
            lines.add(node.lineno)
    return sorted(lines)


def _single_level_positional_offenders(tree: ast.AST) -> list[int]:
    """Return sorted line numbers of single-level integer-literal subscripts.

    A site is any ``Subscript`` whose index is an integer literal -- ``x[i]``.
    This is a superset of :func:`_chained_positional_offenders` (each level of a
    chain ``x[i][j]`` is itself a single-level subscript), so the chained gate
    is strictly stronger; this detector is what powers the #1491 single-level
    ratchet. Pure on its input so a planted fixture can exercise it without
    touching the filesystem.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_int_literal(node.slice):
            lines.add(node.lineno)
    return sorted(lines)


@functools.cache
def _feature_files() -> tuple[Path, ...]:
    """All ``src/notebooklm`` Python files outside the sanctioned decoding packages.

    Cached: the tree is scanned once per test session (the function takes no
    args, so :func:`functools.cache` keys on the empty call and the result is
    shared across the multiple tests that walk the feature tree). Returns a tuple
    so the cached value cannot be mutated by a caller.
    """
    return tuple(
        sorted(
            p
            for p in SRC_ROOT.rglob("*.py")
            if p.relative_to(SRC_ROOT).parts[0] not in SANCTIONED_PACKAGES
        )
    )


def _rel(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


@functools.cache
def _offending_files() -> dict[str, list[int]]:
    """Map ``rel-path -> offending line numbers`` for every feature file that offends.

    Cached: several tests call this, and each call would otherwise re-walk the
    feature tree and re-parse every module's AST. The function takes no args, so
    :func:`functools.cache` memoises the single whole-tree scan and the parse
    work happens exactly once per session. (Callers treat the result as
    read-only.)
    """
    offenders: dict[str, list[int]] = {}
    for path in _feature_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _chained_positional_offenders(tree)
        if lines:
            offenders[_rel(path)] = lines
    return offenders


@functools.cache
def _single_level_offending_files() -> dict[str, list[int]]:
    """Map ``rel-path -> single-level offending line numbers`` for every feature file.

    Cached for the same reason as :func:`_offending_files` -- the #1491
    single-level ratchet tests share one whole-tree scan. Callers treat the
    result as read-only.
    """
    offenders: dict[str, list[int]] = {}
    for path in _feature_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _single_level_positional_offenders(tree)
        if lines:
            offenders[_rel(path)] = lines
    return offenders


def test_no_unbaselined_chained_positional_rpc_indexing() -> None:
    """No feature file outside the allowlist may chain integer-literal subscripts.

    This is the gate: a brand-new file (or a migrated file removed from the
    allowlist) that open-codes ``x[i][j]`` positional descent into an RPC
    payload fails here. Route the descent through ``rpc/_safe_index.safe_index``
    or a ``_row_adapters/`` typed view instead.
    """
    offenders = _offending_files()
    unbaselined = {f: lines for f, lines in offenders.items() if f not in ALLOWLIST}
    assert not unbaselined, (
        "Raw chained positional indexing of RPC payloads (`x[i][j]`) is forbidden "
        "outside src/notebooklm/rpc/ and src/notebooklm/_row_adapters/ (see ADR-0011, "
        "issue #1377). Decode through rpc/_safe_index.safe_index() or a typed "
        "_row_adapters/ view so shape drift RAISES UnknownRPCMethodError instead of "
        "silently degrading to empty/wrong data.\n\n"
        + "\n".join(
            f"  src/notebooklm/{f}:{','.join(map(str, lines))}"
            for f, lines in sorted(unbaselined.items())
        )
    )


def test_no_stale_allowlist_entries() -> None:
    """Every allowlisted file must still offend -- migrated files must be removed.

    Keeps the burndown honest: when a file is migrated behind safe_index / a row
    adapter, it stops offending and must drop off :data:`ALLOWLIST`, which
    re-arms the gate for that file.
    """
    offenders = _offending_files()
    stale = sorted(f for f in ALLOWLIST if f not in offenders)
    assert not stale, (
        "Stale entries in ALLOWLIST -- these files no longer chain positional "
        "subscripts (likely migrated behind safe_index / a row adapter). Remove "
        "them so the gate re-protects them:\n" + "\n".join(f"  {f}" for f in stale)
    )


def test_allowlist_entries_exist() -> None:
    """Every allowlisted path must point at a real file (catches renames/typos)."""
    missing = sorted(f for f in ALLOWLIST if not (SRC_ROOT / f).is_file())
    assert not missing, "ALLOWLIST references nonexistent files:\n" + "\n".join(
        f"  {f}" for f in missing
    )


# ---------------------------------------------------------------------------
# Single-level ratchet (issue #1491)
# ---------------------------------------------------------------------------


def test_no_unbaselined_single_level_positional_rpc_indexing() -> None:
    """No feature file outside the single-level allowlist may add a literal ``x[i]`` read.

    This is the #1491 **burndown ratchet** (introduced the way #1377 introduced
    the chained-descent gate). It fails when a file that is NOT on
    :data:`SINGLE_LEVEL_ALLOWLIST` open-codes a *brand-new* integer-literal
    single-level subscript of an RPC payload. Route the read through a
    ``_row_adapters/`` typed view so the position knowledge lives in one place
    and shape drift RAISES ``UnknownRPCMethodError`` via ``safe_index``.

    Scope (deliberate, like #1377): a *ratchet*, not a closed perimeter. It flags
    only integer-*literal* subscripts (``x[0]``) — a named-constant index
    (``first[TEXT_POS]``) is not detected — and raw reads inside the ~47
    already-allowlisted files are tolerated until each is migrated and dropped
    from the allowlist. The goal is to stop NEW raw positions accruing while the
    existing ones burn down, not to prove "no RPC positions anywhere".
    """
    offenders = _single_level_offending_files()
    unbaselined = {f: lines for f, lines in offenders.items() if f not in SINGLE_LEVEL_ALLOWLIST}
    assert not unbaselined, (
        "Raw single-level positional indexing of RPC payloads (`x[i]`) is forbidden "
        "outside src/notebooklm/rpc/ and src/notebooklm/_row_adapters/ for files not "
        "on SINGLE_LEVEL_ALLOWLIST (see ADR-0011, issue #1491). Decode through a typed "
        "_row_adapters/ view so shape drift RAISES UnknownRPCMethodError instead of "
        "silently degrading to empty/wrong data. NOTE: binding the read to a named local "
        "does NOT satisfy this single-level gate (the local subscript `local[i]` is still "
        "flagged) — move the position knowledge into an adapter; or, for a deliberate "
        "burndown deferral, add the file to SINGLE_LEVEL_ALLOWLIST.\n\n"
        + "\n".join(
            f"  src/notebooklm/{f}:{','.join(map(str, lines))}"
            for f, lines in sorted(unbaselined.items())
        )
    )


def test_no_stale_single_level_allowlist_entries() -> None:
    """Every single-level-allowlisted file must still offend -- migrated files drop off.

    Keeps the #1491 burndown honest: when a file's single-level RPC reads move
    behind a row adapter / named local, it stops offending and must drop off
    :data:`SINGLE_LEVEL_ALLOWLIST`, which re-arms the gate for that file.
    """
    offenders = _single_level_offending_files()
    stale = sorted(f for f in SINGLE_LEVEL_ALLOWLIST if f not in offenders)
    assert not stale, (
        "Stale entries in SINGLE_LEVEL_ALLOWLIST -- these files no longer use a "
        "single-level integer subscript (likely migrated behind a row adapter / named "
        "local). Remove them so the gate re-protects them:\n" + "\n".join(f"  {f}" for f in stale)
    )


def test_single_level_allowlist_entries_exist() -> None:
    """Every single-level-allowlisted path must point at a real file."""
    missing = sorted(f for f in SINGLE_LEVEL_ALLOWLIST if not (SRC_ROOT / f).is_file())
    assert not missing, "SINGLE_LEVEL_ALLOWLIST references nonexistent files:\n" + "\n".join(
        f"  {f}" for f in missing
    )


def test_migrated_chat_wire_is_not_single_level_allowlisted() -> None:
    """``_chat/wire.py`` was migrated behind ``_row_adapters/chat.py`` (issue #1491).

    Pins the headline #1491 outcome: the chat wire parser no longer open-codes
    any single-level RPC-payload subscript, so it is absent from
    :data:`SINGLE_LEVEL_ALLOWLIST` AND from the live offender set -- the gate now
    re-protects it. If a future edit re-introduces a raw ``x[i]`` read there,
    ``test_no_unbaselined_single_level_positional_rpc_indexing`` fails.
    """
    assert "_chat/wire.py" not in SINGLE_LEVEL_ALLOWLIST
    assert "_chat/wire.py" not in _single_level_offending_files()


def test_single_level_detector_flags_and_ignores() -> None:
    """The single-level detector flags ``x[i]`` and ignores string/slice subscripts."""
    flagged = ast.parse(
        "\n".join(
            [
                "a = first[4]",  # single-level int -- flagged
                "b = parts[-1]",  # negative literal -- still positional
                "c = payload[+1]",  # explicit unary-plus -- still positional
                "d = chain[0][1]",  # both levels are single-level subscripts
            ]
        )
    )
    # Line 4 contributes one line number even though it has two subscripts.
    assert _single_level_positional_offenders(flagged) == [1, 2, 3, 4]

    benign = ast.parse(
        "\n".join(
            [
                "x = data['key']",  # string subscript -- not positional
                "y = items[1:]",  # slice -- not an int literal
                "z = flags[True]",  # bool index -- excluded
                "w = [[[source_id]]]",  # list construction, no subscripting
            ]
        )
    )
    assert _single_level_positional_offenders(benign) == []


def test_detector_flags_chained_descent() -> None:
    """The detector flags two-and-three-deep integer-literal descent.

    Both positive and *negative* literal indices count -- ``payload[4][-1]`` is
    just as positional as ``payload[4][3]`` and must not sidestep the gate. An
    explicit unary-plus literal (``payload[+1][0]``) is positional too and must
    not slip through. A call-rooted chain (``parse()[0][1]``) is the same fragile
    descent and is flagged as well.
    """
    tree = ast.parse(
        "\n".join(
            [
                "a = first[4][3]",  # 2-deep, positive
                "b = result[0][2][4]",  # 3-deep (the outer two-level pair fires)
                "c = cite[0][0]",  # 2-deep, repeated index
                "d = payload[4][-1]",  # negative trailing index -- still positional
                "e = payload[-1][0]",  # negative leading index -- still positional
                "f = payload[+1][0]",  # explicit unary-plus -- still positional
                "g = parse()[0][1]",  # call-rooted chained descent -- still positional
            ]
        )
    )
    # Every line contains at least one chained descent.
    assert _chained_positional_offenders(tree) == [1, 2, 3, 4, 5, 6, 7]


def test_detector_flags_unary_plus_index() -> None:
    """An explicit unary-plus literal index must not bypass the gate.

    ``+1`` parses to ``ast.UnaryOp(UAdd, Constant(1))`` -- a positive position
    just like a bare ``1`` -- so ``payload[+1][0]`` is a chained positional
    descent and must be flagged (regression guard for the coderabbit/cubic
    bypass on PR #1390).
    """
    tree = ast.parse("x = payload[+1][0]\n")
    assert _chained_positional_offenders(tree) == [1]


def test_detector_ignores_benign_subscripts() -> None:
    """Single-level, non-int, slice, and list-literal-construction sites are NOT flagged.

    These are the false-positive shapes the gate must tolerate: a single index,
    string/keyword subscripts, slices, and *constructing* nested params with list
    literals (``[[[source_id]]]``) -- which is not subscripting at all.
    """
    benign = "\n".join(
        [
            "x = args[0]",  # single-level int subscript -- allowed
            "y = data['key']['nested']",  # chained, but string keys -- not positional
            "z = items[1:][0]",  # slice then index -- slice is not an int literal
            "p = [[[source_id]]]",  # params construction, no subscripting
            "q = matrix[i][j]",  # variable indices, not literals
            "r = flags[True][False]",  # bool indices must not count as int literals
        ]
    )
    tree = ast.parse(benign)
    assert _chained_positional_offenders(tree) == []


def test_gate_catches_a_planted_offender_in_a_fresh_module() -> None:
    """A would-be new feature module with chained descent is caught by the detector.

    Simulates the gate's real job: a NEW file (not on the allowlist) that
    open-codes ``response[0][1]`` must be rejected.
    """
    tree = ast.parse("def parse(response):\n    return response[0][1]\n")
    assert _chained_positional_offenders(tree) == [2]
