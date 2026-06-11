"""Guard: no pinned recorded values in ``cli_vcr`` assertions (issue #1452).

The ``cli_vcr`` suite (``tests/integration/cli_vcr/``) runs the real CLI →
Client → RPC path against VCR cassettes, but the cassette matcher
(``tests/vcr_config.py`` — ``_rpcids_matcher`` + ``_freq_body_matcher``) keys on
the RPC method id and the decoded body *shape*, **never** on the notebook/source
id in the request. The cli_vcr cassette set is recorded across multiple
notebooks, and ``mock_context`` injects one placeholder id regardless of which
notebook a cassette was recorded against (see ``cli_vcr/_fixtures.py``).

The design contract (issue #1452): **every assertion must survive a re-record
that uses a DIFFERENT notebook with different data.** An assertion that pins a
value which came out of the recorded *response* — a server-returned id, a
recorded title — would break the moment the cassette is re-recorded against
another notebook, even though nothing about the client behaviour changed. That
is exactly the brittle coupling this gate forbids.

The one legitimate equality-against-a-concrete-id is the **input-echo** case: a
mutation command threads the id the test *passed* into its own ``--json`` output,
so ``data["notebook_id"] == MUTATION_NOTEBOOK_ID`` holds for any cassette and
survives any re-record. That comparison is fine because its operand is a
``_fixtures`` placeholder *constant* (a ``Name``/attribute), not an inline
literal — the CLI is echoing the test's input back, not the recording.

So the lint's rule is narrow and unambiguous:

    FAIL if an ``assert`` statement (or a value-comparing ``assert*`` unittest
    call — ``assertEqual`` / ``assertIn`` / ``assertDictEqual`` / …, but not the
    ``assertRaises``-style context managers) contains an **inline string literal
    whose value is opaque-recorded-id-shaped** (see :func:`_is_opaque_recorded_id`).

An opaque-recorded-id literal is the concrete, notebook-tied, re-record-fragile
shape. Three families count (issue #1452, codex review of #1460):

* a **UUID** (``8-4-4-4-12`` hex) — the original case;
* a run of **≥6 consecutive digits** — a numeric recorded id such as the
  mind-map artifact ``47523923`` that slipped the UUID-only lint and is what
  this widening was written to catch;
* a **≥16-char opaque base64/hex blob** — a single high-entropy token (a cursor
  page-token, a content hash) that carries at least one digit and is *not* a
  readable ``snake_case`` / ``kebab-case`` / ``SCREAMING_CASE`` identifier.

The input-echo case never trips this because it compares to a ``_fixtures``
placeholder *name*, not an inline literal — so there is nothing to allow-list in
practice. The shape tests are deliberately tuned so legitimate literals do NOT
match: schema/enum/status values (``"ready"``, ``"NOTEBOOKLM_ERROR"``,
``"synced_to_server"``, ``"briefing_doc"``), type filters (``"mind-map"``), CLI
flags (``"--json"``), small numbers, short tokens, and prose assert-messages all
stay below the digit-run / blob thresholds or read as identifiers. Pinning a
server-returned id is the unambiguous violation worth gating; widening to "any
literal" would fire on every legitimate schema/enum assertion.

This is a forward ratchet: every inline recorded id in the ``cli_vcr`` tests has
been migrated onto ``_fixtures`` placeholder names or replaced with a
shape/invariant assertion, so the gate is GREEN on ``main`` today and stays green
unless someone re-introduces a pinned recorded value. If a genuinely-legitimate
opaque-shaped inline literal ever appears (none is known today), add it to
:data:`ALLOWLIST` with a one-line justification.

Modelled on the AST/path lints in ``tests/_guardrails/`` (e.g.
``test_no_raw_positional_rpc_indexing.py``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TypeGuard

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_VCR_DIR = REPO_ROOT / "tests" / "integration" / "cli_vcr"

# 8-4-4-4-12 hex UUID, anchored to the whole string. A re-record yields a
# different UUID, so any *inline* UUID literal in an assertion is a value pinned
# from a specific recording.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# A run of >=6 consecutive digits anywhere in the literal — a numeric recorded
# id (e.g. the mind-map artifact ``47523923``). Six digits is comfortably above
# any legitimate small number a cli_vcr assertion compares against (exit codes,
# counts, page sizes, HTTP statuses), so it is the re-record-fragile shape.
_NUMERIC_ID_RE = re.compile(r"\d{6,}")

# An opaque base64/hex blob: the whole literal is one >=16-char token drawn from
# the base64/hex alphabet (incl. ``+`` ``/`` ``=`` padding and ``_`` ``-`` of the
# URL-safe variant), anchored so prose/sentence assert-messages (which contain
# spaces) never match.
_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{16,}$")
_HAS_DIGIT_RE = re.compile(r"\d")
# A readable identifier segment is either purely alphabetic (any length, e.g.
# ``synced`` / ``NOTEBOOKLM``) or a short (<=8-char) alphanumeric wordlet (e.g.
# ``v2`` / ``h264`` / ``v1beta1`` / ``x86`` / ``1st``). A blob token split on
# ``_``/``-`` whose every segment reads like this is a field name / enum / version
# string, NOT a recorded id; a high-entropy hash/base64 token has a long
# no-separator alphanumeric run that fails both shapes.
_ALPHA_SEGMENT_RE = re.compile(r"^[A-Za-z]+$")
_SHORT_ALNUM_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


def _is_readable_identifier(value: str) -> bool:
    """True if ``value`` reads as a ``snake_case`` / ``kebab-case`` identifier.

    Splits on ``_``/``-`` and checks every non-empty segment is either purely
    alphabetic (any length) or a short (<=8-char) alphanumeric wordlet. This
    tolerates digits anywhere in a segment, so common identifiers stay readable:
    ``"synced_to_server"``, ``"briefing_doc"``, ``"NOTEBOOKLM_ERROR"``,
    ``"x264_high_profile"``, ``"v1beta1_api_client"`` and ``"x86_64_ubuntu"`` all
    read as identifiers; a hash (``"f8cb37228518a4c33b744"``) or base64 token has
    a long (>8) non-alpha run and does not.
    """
    for seg in re.split(r"[_-]", value):
        if not seg:
            continue
        if not (_ALPHA_SEGMENT_RE.match(seg) or _SHORT_ALNUM_SEGMENT_RE.match(seg)):
            return False
    return True


def _is_opaque_blob(value: str) -> bool:
    """True if ``value`` is a >=16-char high-entropy base64/hex recorded blob.

    Distinguishes an opaque recorded token (a cursor page-token, a content hash)
    from a long-but-readable identifier/field-name/enum value. A blob must:

    * be a single 16+ char token over the base64/hex(-safe) alphabet (anchored,
      so prose assert-messages with spaces never match);
    * carry at least one digit (recorded ids do; pure-word identifiers do not);
    * either use base64 ``+``/``/``/``=`` (never an identifier) or fail the
      readable-identifier shape.
    """
    if not _BLOB_RE.match(value):
        return False
    if not _HAS_DIGIT_RE.search(value):
        return False
    if "=" in value or "+" in value or "/" in value:
        return True
    return not _is_readable_identifier(value)


def _is_opaque_recorded_id(value: str) -> bool:
    """True if ``value`` has the shape of a value pinned from a recording.

    Three families, any of which is re-record-fragile: a UUID, a >=6-digit
    numeric id, or an opaque >=16-char base64/hex blob. Tuned so schema/enum
    literals, CLI flags, type filters, small numbers and prose stay out of scope
    (see the module docstring).
    """
    return (
        bool(_UUID_RE.match(value)) or bool(_NUMERIC_ID_RE.search(value)) or _is_opaque_blob(value)
    )


# Inline opaque-id literals that are legitimately pinned (NOT recorded-response
# values). Empty today: every inline recorded id has been removed, and the
# input-echo case compares to a ``_fixtures`` placeholder *name*, never an inline
# literal. Add an entry as ``"relpath:lineno"`` only with a justifying comment.
ALLOWLIST: frozenset[str] = frozenset()


# ``unittest`` ``assert*`` methods that do NOT compare values for equality:
# context managers (``assertRaises`` / ``assertWarns`` / ``assertLogs`` and
# their ``*Regex`` variants). Every *other* ``assert*`` method
# (``assertEqual``, ``assertIn``, ``assertDictEqual``, …) takes the asserted
# value as a positional arg, so an opaque-id literal in any of those is a pinned
# value and must be flagged. Excluding by this small denylist (rather than
# allow-listing the comparison methods) keeps the gate robust as new
# ``assert*`` helpers appear.
_NON_COMPARISON_ASSERT_METHODS = frozenset(
    {
        "assertRaises",
        "assertRaisesRegex",
        "assertWarns",
        "assertWarnsRegex",
        "assertLogs",
        "assertNoLogs",
    }
)


def _is_opaque_id_literal(node: ast.AST) -> TypeGuard[ast.Constant]:
    """True if ``node`` is a string constant whose value is opaque-recorded-id-shaped.

    A ``TypeGuard`` so callers can read ``node.lineno`` after a positive check
    (it narrows ``ast.AST`` -> ``ast.Constant``, which carries position info).
    """
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _is_opaque_recorded_id(node.value)
    )


def _is_assert_call(node: ast.Call) -> bool:
    """True if ``node`` is a value-comparing ``unittest`` ``assert*`` call.

    Any method whose name starts with ``assert`` (called as ``self.assertX`` or
    a bare ``assertX``) counts, except the context-manager forms in
    :data:`_NON_COMPARISON_ASSERT_METHODS`, which take no asserted *value*.
    """
    func = node.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else None
    )
    return (
        name is not None
        and name.startswith("assert")
        and name not in _NON_COMPARISON_ASSERT_METHODS
    )


class _OpaqueIdAssertVisitor(ast.NodeVisitor):
    """Collect line numbers of opaque-id literals that sit inside an assertion.

    Single-pass over the tree: a depth counter (``_depth``) tracks whether the
    current node is nested inside an ``assert`` statement or an ``assert*``
    call. Any opaque-recorded-id string constant seen while ``_depth > 0`` is a
    value pinned from a specific recording, wherever in the asserted expression
    it sits (a comparison operand, a membership left operand, a set/list member,
    a call arg). Visiting once avoids the nested-``ast.walk`` re-scan of every
    subtree.
    """

    def __init__(self) -> None:
        self.lines: set[int] = set()
        self._depth = 0

    def visit_Assert(self, node: ast.Assert) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        if _is_assert_call(node):
            self._depth += 1
            self.generic_visit(node)
            self._depth -= 1
        else:
            self.generic_visit(node)

    def generic_visit(self, node: ast.AST) -> None:
        if self._depth > 0 and _is_opaque_id_literal(node):
            self.lines.add(node.lineno)
        super().generic_visit(node)


def _opaque_id_literal_lines(tree: ast.AST) -> list[int]:
    """Return sorted line numbers of opaque-id literals inside assertions.

    An "assertion" is an ``assert`` statement or a value-comparing ``assert*``
    unittest call (see :func:`_is_assert_call`). Pure on its input so a planted
    fixture can exercise it without touching the filesystem.
    """
    visitor = _OpaqueIdAssertVisitor()
    visitor.visit(tree)
    return sorted(visitor.lines)


def _cli_vcr_test_files() -> list[Path]:
    """Every ``test_*.py`` under ``tests/integration/cli_vcr/``."""
    return sorted(CLI_VCR_DIR.glob("test_*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _offending_sites() -> dict[str, list[int]]:
    """Map ``relpath -> offending line numbers`` for every cli_vcr test file."""
    offenders: dict[str, list[int]] = {}
    for path in _cli_vcr_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = _rel(path)
        lines = [
            line for line in _opaque_id_literal_lines(tree) if f"{rel}:{line}" not in ALLOWLIST
        ]
        if lines:
            offenders[rel] = lines
    return offenders


def test_no_pinned_recorded_id_literals_in_cli_vcr_asserts() -> None:
    """No ``cli_vcr`` assertion may pin an inline opaque-recorded-id literal.

    A UUID / numeric id / opaque blob came out of a specific recording; pinning
    it breaks the moment the cassette is re-recorded against a different
    notebook. Compare to a ``cli_vcr/_fixtures.py`` placeholder constant instead
    (the input-echo case), or assert the re-record-safe invariant (the id
    *shape*, ``count > 0``, a type-display string) rather than the exact value.
    """
    offenders = _offending_sites()
    assert offenders == {}, (
        "Inline opaque-recorded-id literal(s) found in cli_vcr assertions (issue "
        "#1452). Assertions must survive a re-record against a different "
        "notebook, so a value pinned from the recorded response (a UUID, a "
        "numeric id, or an opaque base64/hex blob) is forbidden. Compare to a "
        "cli_vcr/_fixtures.py placeholder constant (input-echo) or assert the "
        "shape/invariant instead:\n"
        + "\n".join(
            f"  {rel}:{','.join(map(str, lines))}" for rel, lines in sorted(offenders.items())
        )
    )


def test_allowlist_entries_are_well_formed() -> None:
    """Every ALLOWLIST entry must be ``relpath:lineno`` for an existing file.

    Catches typos / renames that would silently weaken the gate (a dangling
    entry can never suppress a real offender, but it also documents intent that
    no longer applies).
    """
    bad: list[str] = []
    for entry in ALLOWLIST:
        rel, _, lineno = entry.partition(":")
        if not lineno.isdigit() or not (REPO_ROOT / rel).is_file():
            bad.append(entry)
    assert bad == [], (
        "Malformed or stale ALLOWLIST entries (want 'relpath:lineno' for an "
        f"existing file): {sorted(bad)}"
    )


def test_detector_flags_pinned_recorded_ids_in_assert() -> None:
    """The detector flags every opaque-recorded-id shape in a value-comparing assertion.

    Covers all three families across the assertion surface: a UUID (either side
    of ``==``, in a set member, via ``assertEqual`` / ``assertIn`` /
    ``assertDictEqual``), a >=6-digit numeric id (the ``47523923`` mind-map case
    the UUID-only lint missed, incl. the ``in result.output`` membership form),
    and an opaque >=16-char base64/hex blob.
    """
    src = "\n".join(
        [
            "assert data['notebook_id'] == 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'",  # 1 UUID
            "assert 'C3F6285F-1709-44C4-9CD6-E95CF0EA4F5E' == data['id']",  # 2 UUID (upper)
            "self.assertEqual(out['id'], 'fdfc8ac4-3237-4f2a-8a79-3e24297a7040')",  # 3 UUID
            "assert src['id'] in {'00000000-0000-0000-0000-000000000000'}",  # 4 UUID (set)
            "self.assertIn('11111111-1111-1111-1111-111111111111', ids)",  # 5 UUID assertIn
            "self.assertDictEqual(d, {'id': '22222222-2222-2222-2222-222222222222'})",  # 6 UUID
            "assert '47523923' in result.output",  # 7 numeric id (the landmine shape)
            "assert data['count'] == '1234567'",  # 8 numeric id (>=6 digits)
            "assert 'MTc4MDEzMzM5OS01ODQyMjkwMDA=' in result.output",  # 9 base64 blob
            "self.assertIn('f8cb37228518a4c33b744ef1', tokens)",  # 10 hex blob
        ]
    )
    assert _opaque_id_literal_lines(ast.parse(src)) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


def test_detector_ignores_re_record_safe_assertions() -> None:
    """Placeholder names, schema/enum literals, and non-assert ids are NOT flagged.

    These are the re-record-safe shapes the gate must tolerate:

    * comparison/membership against a ``_fixtures`` placeholder *name* (the
      input-echo case — the ``Name`` operand carries the recorded-safe id);
    * schema/enum/status/field literals (``"pass"`` / ``"NOTEBOOKLM_ERROR"`` /
      ``"synced_to_server"`` / ``"briefing_doc"``), type filters (``"mind-map"``),
      version-bearing identifiers (``"v1beta1_api_client"`` / ``"x86_64_ubuntu"``,
      the digit-in-segment cases from PR #1461 review), CLI flags (``"--json"``),
      small numbers (``"200"``) and prose assert-messages — none reach the
      digit-run / blob thresholds or all read as identifiers;
    * an opaque id literal that is *not* inside an assertion (a command argument
      passed to ``runner.invoke`` or a module-level placeholder definition).
    """
    benign = "\n".join(
        [
            "assert data['notebook_id'] == MUTATION_NOTEBOOK_ID",  # input-echo (Name)
            "assert ARTIFACT_NOTEBOOK_ID in result.output",  # input-echo membership (Name)
            "assert data['checks']['auth']['status'] == 'pass'",  # schema enum
            "assert data.get('code') == 'NOTEBOOKLM_ERROR'",  # error-code enum (16-char ident)
            "assert data.get('synced_to_server') is True",  # field name (16-char ident)
            "assert data['type_id'] == 'briefing_doc'",  # report subtype enum
            "assert data['client'] == 'v1beta1_api_client'",  # version-bearing identifier
            "assert data['image'] == 'x86_64_ubuntu_image'",  # arch identifier (digit-in-segment)
            "assert 'mind-map' in args",  # kebab type filter
            "assert data['action'] == 'delete'",  # command action",
            "assert data.get('language') == 'en'",  # input-echo language code
            "assert result.exit_code == 0, 'expected the command to succeed cleanly'",  # prose msg
            "assert data.get('added_user') == VCR_SHARE_EMAIL",  # input-echo (Name)
            "assert 'Mind Map' in result.output",  # type-display marker (the landmine fix)
            "assert data['count'] == '200'",  # small number
            "result = runner.invoke(cli, ['source', 'get', 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'])",
            "PLACEHOLDER_NOTEBOOK_ID = 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'",
            # A context-manager assert takes no asserted *value* — a UUID inside
            # the managed block is a command arg, not a pinned comparison.
            "with self.assertRaises(ValueError):\n"
            "    runner.invoke(cli, ['source', 'get', 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'])",
        ]
    )
    assert _opaque_id_literal_lines(ast.parse(benign)) == []
