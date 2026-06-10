"""Guards: no raw positional RPC indexing in feature code + no raw-payload
ingress above the facade.

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

The enforcement is **LAYERED** -- different layers get the gate that actually
carries signal for them:

* **BELOW the facade (the feature/decode layer: ``_chat/``, ``_artifact/``,
  ``_source/``, ``_types/``, the ``_*.py`` facade internals, ...).** This is
  where decoded ``batchexecute`` payloads legitimately flow, so positional
  decode must live behind ``rpc/`` + ``_row_adapters/`` + ``safe_index``. Both
  positional-indexing gates apply here: the chained-descent gate and the
  single-level ``name[int]`` ratchet (whose allowlist is the remaining
  burndown).

* **ABOVE the facade (``cli/``, ``_app/``).** These layers must have **ZERO
  raw-payload access** -- they consume typed facade returns only. That
  invariant is enforced by (i) the payload-INGRESS gate
  (:func:`test_no_raw_payload_ingress_above_facade`): the raw-returning facade
  methods are enumerated in :data:`RAW_PAYLOAD_FACADE_METHODS` (a maintained
  denylist -- the public raw returners found by introspection plus the
  ``getattr``-accessed ``_list_for_download`` prefetch seam), and reaching any
  of them from ``cli/`` or ``_app/`` -- via ANY attribute reference (called or
  merely bound) OR a ``getattr`` string-literal -- fails the gate, with one
  documented per-method opaque-passthrough exemption
  (:data:`INGRESS_EXEMPTIONS`); (ii) the chained gate, which stays
  FULL-SCOPE over the whole feature tree; and (iii) the typed facade returns
  themselves (mypy: you cannot subscript a ``Note``). The type-blind
  ``name[int]`` scan carries **no signal** above the facade -- at rescope time
  every one of its hits there was a benign Python idiom (``matches[0]``-style
  sequence reads, string parsing) -- so the single-level gate EXCLUDES those
  packages (:data:`SINGLE_LEVEL_EXCLUDED_PACKAGES`) rather than grandfathering
  20 files of noise.

* **``_auth/``, ``utils.py``, ``_version_check.py``.** Never see
  ``batchexecute`` payloads by construction (they handle cookies / argv /
  constants), so the single-level scan excludes them too
  (:data:`SINGLE_LEVEL_EXCLUDED_PACKAGES` / :data:`SINGLE_LEVEL_EXCLUDED_FILES`);
  the chained gate still covers them.

This module therefore runs **three** AST gates:

1. **Chained descent (issue #1377, FULL-SCOPE).** A ``Subscript`` indexed by an
   integer literal whose *own value* is another integer-literal ``Subscript``
   -- i.e. a two-or-more-deep positional descent like ``x[i][j]``
   (``first[4][3]``, ``result[0][2][4]``, ``cite[0][0]``). This is the most
   fragile "deep descent into an RPC payload" shape, and its :data:`ALLOWLIST`
   is **empty** -- the #1377 burndown migrated every chained offender, so the
   chained gate re-protects the whole feature tree (above AND below the
   facade) with no exceptions.

2. **Single-level descent (issue #1491, narrowed in #1501, rescoped BELOW the
   facade).** A single ``Subscript`` indexed by an integer literal whose
   *value is a bare local* ``Name`` -- the RPC-payload shape ``name[int]``
   (``first[4]`` / ``cite[1]`` / ``passage_data[0]``). That is exactly how
   un-named row-position knowledge of an RPC payload leaks past the chained
   gate, because a decoded ``batchexecute`` list is always bound to a local
   before it is walked. Attribute/call subscripts (``sys.version_info[0]``,
   ``url.split("@", 1)[0]``) are **excluded as structurally-benign** -- they
   are never positional descents into an RPC payload -- so the gate is precise
   and benign indexing need not be grandfathered. The gate works as a
   **ratchet** over the below-facade scope: files that already open-code a
   ``name[int]`` RPC read are *baselined* into
   :data:`SINGLE_LEVEL_ALLOWLIST` (so the gate is green on ``main`` today),
   but a *new* ``name[int]`` read in an in-scope file NOT on that list fails
   the gate. The burndown that drains :data:`SINGLE_LEVEL_ALLOWLIST` is
   tracked by #1501.

3. **Raw-payload ingress (above-facade).** Payloads enter ``cli/`` / ``_app/``
   through the raw-returning facade methods enumerated in
   :data:`RAW_PAYLOAD_FACADE_METHODS`; reaching one of them from those
   packages -- via any attribute reference (called or bound) or a ``getattr``
   string-literal -- fails :func:`test_no_raw_payload_ingress_above_facade`,
   except for the documented per-method opaque-passthrough exemptions
   (:data:`INGRESS_EXEMPTIONS`).

A string/slice subscript (``d["k"]``, ``s[1:]``) is ignored by the positional
gates.

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

# Scope carve-out for the SINGLE-LEVEL gate only (the chained gate stays
# full-scope). ``cli/`` and ``_app/`` sit ABOVE the facade and must have zero
# raw-payload access -- that invariant is enforced precisely by the
# payload-ingress gate below + the (full-scope) chained gate + the typed facade
# returns, NOT by the type-blind ``name[int]`` scan (whose hits there were all
# benign ``matches[0]``-style idioms / string parsing, verified at rescope
# time). ``_auth/``, ``utils.py`` and ``_version_check.py`` never see
# ``batchexecute`` payloads by construction (cookies / argv / constants).
SINGLE_LEVEL_EXCLUDED_PACKAGES = frozenset({"cli", "_app", "_auth"})
SINGLE_LEVEL_EXCLUDED_FILES = frozenset({"utils.py", "_version_check.py"})

# Baseline of BELOW-FACADE feature files that open-code a *single-level*
# integer-literal subscript (``x[i]``) of a decoded RPC payload (issue #1491).
# Many of these reads are genuine but already-guarded inner reads -- the
# single-level gate is a RATCHET, not an immediate ban: these files are
# grandfathered so the gate is green on ``main``, but a single-level subscript
# in any in-scope file NOT on this list fails the gate. Above-facade files
# (``cli/`` / ``_app/`` / ``_auth/`` / ``utils.py`` / ``_version_check.py``)
# are excluded from the scan entirely (see the scope constants above) and MUST
# NOT appear here (pinned by
# ``test_single_level_allowlist_has_no_above_facade_entries``).
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
        "_artifact/downloads.py",
        "_artifact/formatters.py",
        "_artifact/listing.py",
        "_artifact/polling.py",
        "_artifacts.py",
        "_chat/api.py",
        "_chat/notes.py",
        "_labels.py",
        "_mind_maps_api.py",
        "_note_service.py",
        "_notebooks.py",
        "_research.py",
        "_source/add.py",
        "_source/content.py",
        "_source/listing.py",
        "_source/upload.py",
        "_types/artifacts.py",
        "_types/notebooks.py",
        "_types/sharing.py",
        "_types/sources.py",
    }
)

# The enumerated facade methods that return RAW (untyped / positional) RPC
# payloads instead of typed objects. Verified by introspection at
# gate-introduction time: the public ``notebooks.get_raw -> Any`` and
# ``notes.list_mind_maps -> list[Any]`` (every other PUBLIC facade method
# returns typed objects), plus the private ``artifacts._list_for_download``
# prefetch seam, which ``_app/download.py`` reaches via
# ``getattr(..., "_list_for_download", None)`` (issue #1488) -- a dynamic
# access the ``_``-boundary lint cannot see, so the ingress gate names it
# here. This is a DENYLIST, not a proven-complete inventory: the gate flags
# attribute calls and ``getattr`` string-literals naming these methods, so if
# a NEW raw-returning facade method is ever added, it must be added to this
# set in the same PR for the gate to keep covering it.
RAW_PAYLOAD_FACADE_METHODS = frozenset({"get_raw", "list_mind_maps", "_list_for_download"})

# Above-facade files exempt from the ingress gate, with their contract:
#
# * ``_app/download.py`` -- the #1488 single-list prefetch: it receives the raw
#   studio/mind-map rows from ``artifacts._list_for_download`` and threads them
#   straight back into the facade's ``download_<x>(..., artifacts_data=/
#   mind_maps=)`` kwargs as an OPAQUE PASSTHROUGH. It must never index or
#   decode those rows -- the moment it needs to look inside them, that decoding
#   must move below the facade (a typed adapter / facade method), not be done
#   in ``_app``. The exemption covers the handoff, not payload access.
# Per-METHOD exemptions: ``file -> {exempted method names}``. ONLY the named
# seam is exempted in that file -- any OTHER denylisted method reached from the
# same file still fails the ingress gate (deliberately not a file-level skip,
# so the exemption cannot widen silently).
INGRESS_EXEMPTIONS: dict[str, frozenset[str]] = {
    "_app/download.py": frozenset({"_list_for_download"}),
}

# The packages that sit ABOVE the facade: transport adapters + transport-neutral
# business logic. They consume typed facade returns only.
ABOVE_FACADE_PACKAGES = ("cli", "_app")


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
    """Return sorted line numbers of the RPC-payload shape ``name[int]``.

    A site is a ``Subscript`` indexed by an integer literal whose *value is a
    bare* :class:`ast.Name` -- i.e. ``data[0]`` / ``result[2]``, a positional
    read of a **decoded RPC payload bound to a local name**. That is the shape
    the #1491 single-level ratchet targets: a genuine RPC-payload position read
    is always rooted at a local variable, because the decoded ``batchexecute``
    list is bound to a name before it is walked.

    Subscripts whose value is an :class:`ast.Attribute` (``sys.version_info[0]``,
    ``e.args[0]``, ``context.pages[0]``), an :class:`ast.Call`
    (``url.split("@", 1)[0]``, ``Path(...).parents[3]``), or another
    :class:`ast.Subscript` are **excluded as structurally-benign**: every such
    site in the feature tree is a stdlib / string / path read, never a
    positional descent into an RPC payload (which is *always* bound to a local
    ``Name`` first). Excluding them keeps the ratchet precise -- it flags only
    the ``name[int]`` shape that re-scatters RPC-row position knowledge -- so
    benign attribute/call indexing does not have to be grandfathered into
    :data:`SINGLE_LEVEL_ALLOWLIST`.

    Note this is therefore NOT a strict superset of
    :func:`_chained_positional_offenders`: the *inner* level of a chain
    ``x[i][j]`` (where the outer value is itself a ``Subscript``) is excluded
    here, but the chained gate -- which is the strictly-stronger gate for that
    deep-descent shape -- still catches it. Pure on its input so a planted
    fixture can exercise it without touching the filesystem.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and _is_int_literal(node.slice)
            and isinstance(node.value, ast.Name)
        ):
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


def _is_single_level_excluded(rel: str) -> bool:
    """True when ``rel`` is outside the single-level gate's below-facade scope.

    Above-facade packages (``cli/`` / ``_app/`` / ``_auth/``) and the
    payload-free top-level files (``utils.py`` / ``_version_check.py``) are
    excluded from the SINGLE-LEVEL scan only -- the chained gate stays
    full-scope (see the module docstring for the layered invariant).
    """
    return rel.split("/", 1)[0] in SINGLE_LEVEL_EXCLUDED_PACKAGES or (
        rel in SINGLE_LEVEL_EXCLUDED_FILES
    )


@functools.cache
def _single_level_offending_files() -> dict[str, list[int]]:
    """Map ``rel-path -> single-level offending line numbers`` for in-scope files.

    Cached for the same reason as :func:`_offending_files` -- the #1491
    single-level ratchet tests share one whole-tree scan. Callers treat the
    result as read-only. Scope: below-facade feature files only
    (:func:`_is_single_level_excluded` filters the above-facade /
    payload-free-by-construction surfaces out of THIS gate; the chained gate
    keeps scanning them).
    """
    offenders: dict[str, list[int]] = {}
    for path in _feature_files():
        rel = _rel(path)
        if _is_single_level_excluded(rel):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _single_level_positional_offenders(tree)
        if lines:
            offenders[rel] = lines
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
    """No in-scope feature file outside the allowlist may add a literal ``x[i]`` read.

    This is the #1491 **burndown ratchet** (introduced the way #1377 introduced
    the chained-descent gate). It fails when a below-facade file that is NOT on
    :data:`SINGLE_LEVEL_ALLOWLIST` open-codes a *brand-new* integer-literal
    single-level subscript of an RPC payload. Route the read through a
    ``_row_adapters/`` typed view so the position knowledge lives in one place
    and shape drift RAISES ``UnknownRPCMethodError`` via ``safe_index``.

    Scope (deliberate, like #1377): a *ratchet* over the BELOW-FACADE layer
    only, not a closed perimeter. Above-facade packages (``cli/`` / ``_app/``)
    and the payload-free-by-construction surfaces (``_auth/``, ``utils.py``,
    ``_version_check.py``) are excluded -- the ``name[int]`` shape is type-blind
    and carries no signal there; their zero-raw-payload invariant is enforced
    by the precise ingress gate
    (:func:`test_no_raw_payload_ingress_above_facade`), the full-scope chained
    gate, and the typed facade returns. Within scope, the gate flags only the
    RPC-payload shape ``name[int]`` -- an integer-*literal* subscript of a bare
    local :class:`ast.Name` (``data[0]``). A named-constant index
    (``first[TEXT_POS]``) is not detected, and attribute/call subscripts
    (``sys.version_info[0]``, ``url.split("@", 1)[0]``) are excluded as
    structurally-benign (see :func:`_single_level_positional_offenders`). Raw
    reads inside the already-allowlisted files are tolerated until each is
    migrated and dropped from the allowlist. The goal is to stop NEW raw RPC-row
    positions accruing while the existing ones burn down, not to prove "no
    integer subscripts anywhere".
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


def test_single_level_allowlist_has_no_above_facade_entries() -> None:
    """No allowlist entry may live in the single-level gate's excluded scope.

    The above-facade packages (``cli/`` / ``_app/`` / ``_auth/``) and the
    payload-free top-level files (``utils.py`` / ``_version_check.py``) are
    excluded from the single-level scan, so an allowlist entry there would be
    dead weight that *looks* like sanctioned raw-payload access. Guards against
    re-adding the 20 entries the rescope removed.
    """
    out_of_scope = sorted(f for f in SINGLE_LEVEL_ALLOWLIST if _is_single_level_excluded(f))
    assert not out_of_scope, (
        "SINGLE_LEVEL_ALLOWLIST entries outside the gate's below-facade scope "
        "(cli/ / _app/ / _auth/ / utils.py / _version_check.py are excluded from "
        "the single-level scan; above-facade raw-payload access is governed by "
        "the ingress gate instead):\n" + "\n".join(f"  {f}" for f in out_of_scope)
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
    """The single-level detector flags ``name[i]`` and ignores string/slice subscripts."""
    flagged = ast.parse(
        "\n".join(
            [
                "a = first[4]",  # name[int] -- flagged
                "b = parts[-1]",  # negative literal of a name -- still positional
                "c = payload[+1]",  # explicit unary-plus of a name -- still positional
                "d = chain[0][1]",  # inner ``chain[0]`` is name-rooted -- flagged
            ]
        )
    )
    # Line 4 contributes one line number: the OUTER ``chain[0][1]`` has a
    # ``Subscript`` value (excluded), but its INNER ``chain[0]`` is name-rooted
    # and fires (the chained gate is the stronger gate for the outer descent).
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


def test_single_level_detector_targets_name_rooted_rpc_shape_only() -> None:
    """The single-level detector flags only ``name[int]``, not attribute/call subscripts.

    Pins the #1501 narrowing: by current codebase convention a genuine
    RPC-payload position read is rooted at a local ``Name`` (``data[0]`` /
    ``result[2]``) — every decoded batchexecute payload is bound to a local
    before it is walked (verified across the whole feature tree at narrowing
    time: all 15 non-``Name`` integer subscripts were benign stdlib/string/path
    reads). The ratchet therefore targets exactly that shape and *excludes*
    integer subscripts of an attribute (``sys.version_info[0]``, ``e.args[0]``,
    ``ctx.pages[0]``) or a call (``Path(...).parents[3]``, ``s.split("@", 1)[0]``,
    ``f()[0]``). This is a convention-backed scope, not an absolute guarantee —
    a future ``self._payload[0]`` or ``parse_rpc()[0]`` would evade it; if that
    idiom ever appears in feature code, widen the detector rather than adopting
    the idiom.
    """
    excluded = ast.parse(
        "\n".join(
            [
                "a = sys.version_info[0]",  # attribute value -- excluded
                "b = e.args[0]",  # attribute value -- excluded
                "c = ctx.pages[0]",  # attribute value -- excluded
                "d = Path(__file__).resolve().parents[3]",  # call value -- excluded
                "e = email.split('@', 1)[0]",  # call value -- excluded
                "f = parse()[0]",  # call value -- excluded
            ]
        )
    )
    assert _single_level_positional_offenders(excluded) == []

    rpc_shape = ast.parse(
        "\n".join(
            [
                "data = decode(raw)",
                "a = data[0]",  # name[int] -- flagged (an RPC-row read)
                "b = result[2]",  # name[int] -- flagged
            ]
        )
    )
    assert _single_level_positional_offenders(rpc_shape) == [2, 3]


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


# ---------------------------------------------------------------------------
# Raw-payload ingress gate (above the facade)
# ---------------------------------------------------------------------------


def _raw_payload_ingress_offenders(tree: ast.AST) -> list[tuple[int, str]]:
    """Return sorted ``(line, method)`` pairs reaching a raw-returning facade method.

    Two site shapes are flagged:

    * ANY :class:`ast.Attribute` whose ``attr`` is in
      :data:`RAW_PAYLOAD_FACADE_METHODS` -- called
      (``client.notes.list_mind_maps(nb)``) or merely *referenced/bound*
      (``f = client.notes.list_mind_maps`` and later ``await f(nb)``). Flagging
      the bare reference closes the bind-then-call evasion the call-only
      pattern allowed;
    * a ``getattr(<anything>, "<name>", ...)`` call whose second argument is a
      string literal in :data:`RAW_PAYLOAD_FACADE_METHODS` -- the dynamic form
      (``f = getattr(x, "_list_for_download", None)``) whose later ``f(...)``
      call is an :class:`ast.Name` call invisible to the attribute pattern.
      Flagging the ``getattr`` catches the seam at its point of acquisition.

    Matching is name-based and deliberately receiver-blind: ANY
    ``something.get_raw`` attribute (or ``getattr`` naming a denylisted
    method) is flagged regardless of receiver. That over-match is accepted --
    nothing else in ``cli/`` / ``_app/`` defines those names, and a false
    positive is a loud, cheap rename rather than a silent payload leak. A
    ``getattr`` whose name argument is not a literal (``getattr(x, name)``)
    is NOT detected -- if that idiom ever appears in ``cli/`` / ``_app/``,
    widen the detector rather than adopting the idiom. Pure on its input so
    the planted self-check can exercise it without touching the filesystem.
    """
    sites: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in RAW_PAYLOAD_FACADE_METHODS:
            sites.add((node.lineno, node.attr))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in RAW_PAYLOAD_FACADE_METHODS
        ):
            sites.add((node.lineno, node.args[1].value))
    return sorted(sites)


def test_no_raw_payload_ingress_above_facade() -> None:
    """``cli/`` and ``_app/`` must never reach a raw-returning facade method.

    Raw ``batchexecute`` payloads enter the above-facade layers through the
    facade methods enumerated in :data:`RAW_PAYLOAD_FACADE_METHODS` (the
    public raw returners found by introspection plus the ``getattr``-accessed
    ``_list_for_download`` prefetch seam). Coverage is the enumerated names
    via attribute calls AND ``getattr`` string-literals -- a denylist kept
    current by the add-it-in-the-same-PR rule, not a proven-complete
    inventory. With zero un-exempted sites, the above-facade layers hold no
    raw payload to mis-index -- which is why the type-blind single-level gate
    can exclude them. The documented per-method exemption is
    :data:`INGRESS_EXEMPTIONS` (``_app/download.py`` -> ``_list_for_download``
    only): it ferries the #1488 prefetch rows as an opaque passthrough and must
    never index/decode them (decoding requires moving below the facade); any
    OTHER denylisted method in that file still fails here.
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    for pkg in ABOVE_FACADE_PACKAGES:
        for path in sorted((SRC_ROOT / pkg).rglob("*.py")):
            rel = _rel(path)
            exempt = INGRESS_EXEMPTIONS.get(rel, frozenset())
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sites = [
                (line, method)
                for line, method in _raw_payload_ingress_offenders(tree)
                if method not in exempt
            ]
            if sites:
                offenders[rel] = sites
    assert not offenders, (
        "Raw-payload INGRESS above the facade: cli/ and _app/ must consume TYPED "
        "facade methods (notes.get_or_none, mind_maps.list_note_backed, "
        "artifacts.list, ...) -- raw batchexecute payloads must not cross the "
        "facade boundary. Replace the access with a typed facade method (or add "
        "one). If a NEW raw-returning facade method was added, add it to "
        "RAW_PAYLOAD_FACADE_METHODS in the same PR so this gate keeps covering "
        "it; an opaque-passthrough seam needs a per-method INGRESS_EXEMPTIONS "
        "entry with a documented contract.\n\n"
        + "\n".join(
            f"  src/notebooklm/{f}: " + ", ".join(f"{ln}({m})" for ln, m in sites)
            for f, sites in sorted(offenders.items())
        )
    )


def test_ingress_exemptions_exist_and_still_use_exactly_their_seam() -> None:
    """Every per-method exemption points at a real file still using THAT seam.

    Self-draining, like the allowlists: when ``_app/download.py`` stops using
    the ``getattr(..., "_list_for_download")`` prefetch seam (e.g. the #1488
    handoff moves below the facade), its exemption must be removed so the gate
    re-protects the file. Per-method: the exempted file using any OTHER
    denylisted method is caught by the MAIN gate (the exemption filters only
    its named seam), so this check only needs to police staleness.
    """
    for rel, exempt_methods in sorted(INGRESS_EXEMPTIONS.items()):
        path = SRC_ROOT / rel
        assert path.is_file(), f"INGRESS_EXEMPTIONS references a nonexistent file: {rel}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used = {method for _line, method in _raw_payload_ingress_offenders(tree)}
        stale = sorted(exempt_methods - used)
        assert not stale, (
            f"Stale INGRESS_EXEMPTIONS entry: src/notebooklm/{rel} no longer "
            f"reaches {stale} -- remove the exempted method(s) so the ingress "
            "gate re-protects the file."
        )


def test_ingress_detector_flags_and_ignores() -> None:
    """The ingress detector flags raw-facade access and ignores typed-facade calls.

    Flagged: ANY attribute reference to ``.list_mind_maps`` / ``.get_raw`` --
    called, on an unrelated receiver (the documented receiver-blind
    over-match), or merely BOUND without a call (``f = client.notes.
    list_mind_maps``: the bind-then-call evasion the call-only pattern allowed)
    -- and the ``getattr`` string-literal form that binds a denylisted method
    to a local name (the ``_app/download.py`` #1488 seam shape, whose later
    bound-name call the attribute pattern cannot see).
    Ignored: typed facade calls (``notes.get_or_none`` / ``mind_maps.list`` /
    ``mind_maps.list_note_backed`` / ``artifacts.list``), a bare
    ``get_raw(...)`` name call (not an attribute), and a ``getattr`` naming a
    non-denylisted attribute.
    """
    flagged = ast.parse(
        "\n".join(
            [
                "async def probe(client, facade, foo, nb, kind):",
                "    mm = await client.notes.list_mind_maps(nb)",  # raw facade call
                "    data = client.notebooks.get_raw(nb)",  # raw facade call
                "    x = foo.get_raw(nb)",  # receiver-blind by design -- still flagged
                # the #1488 seam shape: getattr-bind, then call the bound Name.
                '    lfd = getattr(facade.artifacts, "_list_for_download", None)',
                "    rows = await lfd(nb, kind)",  # bound-Name call -- invisible...
                # bind-then-call evasion: the bare reference itself is flagged.
                "    f = client.notes.list_mind_maps",
                "    later = await f(nb)",  # ...because this Name call is invisible
            ]
        )
    )
    # ...so the ACQUISITION sites fire (getattr line 5; bare reference line 7) --
    # the bound-Name calls on lines 6/8 are undetectable, which is exactly why
    # references are flagged at their source.
    assert _raw_payload_ingress_offenders(flagged) == [
        (2, "list_mind_maps"),
        (3, "get_raw"),
        (4, "get_raw"),
        (5, "_list_for_download"),
        (7, "list_mind_maps"),
    ]

    benign = ast.parse(
        "\n".join(
            [
                "async def probe(client, obj, nb, note_id):",
                "    n = await client.notes.get_or_none(nb, note_id)",  # typed facade
                "    maps = await client.mind_maps.list(nb)",  # typed facade
                "    nb_maps = await client.mind_maps.list_note_backed(nb)",  # typed facade
                "    arts = await client.artifacts.list(nb)",  # typed facade
                "    y = get_raw(nb)",  # bare Name call, not an attribute
                '    w = getattr(obj, "list", None)',  # getattr of a typed method
            ]
        )
    )
    assert _raw_payload_ingress_offenders(benign) == []
