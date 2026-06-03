"""Guard: no raw *chained* positional indexing of RPC payloads in feature code.

Google's ``batchexecute`` responses are positional lists (the project's #1
standing risk -- the shape can move without notice). The sanctioned places to
decode those positional structures are:

* ``src/notebooklm/rpc/`` -- the RPC protocol layer (encoder/decoder/safe_index),
  the home of ``safe_index`` itself; and
* ``src/notebooklm/_row_adapters/`` -- the typed row views (``ArtifactRow`` /
  ``NoteRow`` / ``SourceRow``) that centralise position knowledge behind named
  properties.

Everywhere else, walking a decoded payload with a hand-rolled *chain* of
integer-literal subscripts (``first[4][3]``, ``result[0][2][4]``,
``cite[0][0]``) re-scatters the position knowledge the adapters exist to
contain, and -- per **ADR-011** -- routinely *swallows* shape drift to an
empty/wrong value behind ``try/except (IndexError, TypeError)`` instead of
raising ``UnknownRPCMethodError`` via ``safe_index``.

This AST lint forbids the anti-pattern: a ``Subscript`` indexed by an integer
literal whose *own value* is another integer-literal ``Subscript`` -- i.e. a
two-or-more-deep positional descent like ``x[i][j]``. Single-level ``x[i]``
indexing is intentionally **not** flagged (it is too common and too benign --
``args[0]``, ``parts[-1]`` -- to gate without noise); the chained form is the
fragile "deep descent into an RPC payload" shape this gate targets. A
string/slice subscript (``d["k"]``, ``s[1:]``) is likewise ignored.

This is the durable GATE for issue #1377. The current offenders are *baselined*
into :data:`ALLOWLIST` so the gate is green on ``main`` today; the burndown that
drains that list (migrating each file behind ``_row_adapters/`` + ``safe_index``)
is tracked as follow-up issue #1389. New feature files start gated -- adding a
fresh chained positional descent outside ``rpc/`` / ``_row_adapters/`` fails the
gate unless the file is on the allowlist.

The allowlist is self-draining: :func:`test_no_stale_allowlist_entries` fails if
an allowlisted file no longer contains any chained positional descent, so once a
file is migrated it must be removed from the list (the gate then re-protects it).
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

# Baseline of feature files that currently open-code chained positional descent
# into RPC payloads (issue #1377). The follow-up burndown (#1389) migrates each
# behind ``_row_adapters/`` + ``safe_index`` and removes it from this list; a
# removed file is then re-protected by the gate. Keep this list sorted; entries
# are ``Path``-relative-to-``src/notebooklm`` in posix form.
#
# DO NOT add new entries to grow the debt -- a new offender means new code that
# should decode through ``safe_index`` / a row adapter instead.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "_artifact/listing.py",
        "_artifacts.py",
        "_chat/wire.py",
        "_mind_maps_api.py",
        "_note_service.py",
        "_research.py",
        "_research_task_parser.py",
        "_source/content.py",
        "_types/artifacts.py",
        "_types/notebooks.py",
        "_types/notes.py",
        "_types/sharing.py",
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
        "outside src/notebooklm/rpc/ and src/notebooklm/_row_adapters/ (see ADR-011, "
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
