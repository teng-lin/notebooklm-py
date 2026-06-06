"""Guard: no pinned recorded values in ``cli_vcr`` assertions (issue #1452).

The ``cli_vcr`` suite (``tests/integration/cli_vcr/``) runs the real CLI →
Client → RPC path against VCR cassettes, but the cassette matcher
(``tests/vcr_config.py`` — ``_rpcids_matcher`` + ``_freq_body_matcher``) keys on
the RPC method id and the decoded body *shape*, **never** on the notebook/source
id in the request. The 105 cassettes were recorded against 15 distinct
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
    ``assertRaises``-style context managers) contains an **inline UUID-shaped
    string literal**.

A UUID literal is the concrete, notebook-tied, re-record-fragile shape. The
input-echo case never trips this because it compares to a ``_fixtures``
placeholder *name*, not an inline literal — so there is nothing to allow-list in
practice. Schema/enum literals (``"pass"``, ``"RATE_LIMITED"``, ``"delete"``)
and input-echo string literals (a language code, an email the test passed) are
**not** UUIDs and are intentionally out of scope: pinning a server-returned UUID
is the unambiguous violation worth gating, and widening to "any literal" would
fire on every legitimate schema/enum assertion.

This is a forward ratchet: Phase 1 (#1458) already migrated every inline UUID in
the ``cli_vcr`` tests onto ``_fixtures`` placeholder names, so the gate is GREEN
on ``main`` today and stays green unless someone re-introduces a pinned recorded
value. If a genuinely-legitimate inline UUID literal ever appears (none is known
today), add it to :data:`ALLOWLIST` with a one-line justification.

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

# ``unittest`` ``assert*`` methods that do NOT compare values for equality:
# context managers (``assertRaises`` / ``assertWarns`` / ``assertLogs`` and
# their ``*Regex`` variants). Every *other* ``assert*`` method
# (``assertEqual``, ``assertIn``, ``assertDictEqual``, …) takes the asserted
# value as a positional arg, so a UUID literal in any of those is a pinned
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

# Inline UUID literals that are legitimately pinned (NOT recorded-response
# values). Empty today: Phase 1 (#1458) removed every inline UUID, and the
# input-echo case compares to a ``_fixtures`` placeholder *name*, never an inline
# literal. Add an entry as ``"relpath:lineno"`` only with a justifying comment.
ALLOWLIST: frozenset[str] = frozenset()


def _is_uuid_literal(node: ast.AST) -> TypeGuard[ast.Constant]:
    """True if ``node`` is a string constant whose value is UUID-shaped.

    A ``TypeGuard`` so callers can read ``node.lineno`` after a positive check
    (it narrows ``ast.AST`` -> ``ast.Constant``, which carries position info).
    """
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(_UUID_RE.match(node.value))
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


class _UUIDAssertVisitor(ast.NodeVisitor):
    """Collect line numbers of inline UUID literals that sit inside an assertion.

    Single-pass over the tree: a depth counter (``_depth``) tracks whether the
    current node is nested inside an ``assert`` statement or an ``assert*``
    call. Any UUID-shaped string constant seen while ``_depth > 0`` is a value
    pinned from a specific recording, wherever in the asserted expression it
    sits (a comparison operand, a set/list member, a call arg). Visiting once
    avoids the nested-``ast.walk`` re-scan of every subtree.
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
        if self._depth > 0 and _is_uuid_literal(node):
            self.lines.add(node.lineno)
        super().generic_visit(node)


def _uuid_literal_lines(tree: ast.AST) -> list[int]:
    """Return sorted line numbers of inline UUID literals inside assertions.

    An "assertion" is an ``assert`` statement or a value-comparing ``assert*``
    unittest call (see :func:`_is_assert_call`). Pure on its input so a planted
    fixture can exercise it without touching the filesystem.
    """
    visitor = _UUIDAssertVisitor()
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
        lines = [line for line in _uuid_literal_lines(tree) if f"{rel}:{line}" not in ALLOWLIST]
        if lines:
            offenders[rel] = lines
    return offenders


def test_no_pinned_uuid_literals_in_cli_vcr_asserts() -> None:
    """No ``cli_vcr`` assertion may pin an inline UUID-shaped literal.

    A UUID came out of a specific recording; pinning it breaks the moment the
    cassette is re-recorded against a different notebook. Compare to a
    ``cli_vcr/_fixtures.py`` placeholder constant instead (the input-echo case),
    or assert the re-record-safe invariant (UUID *shape*, ``count > 0``) rather
    than the exact value.
    """
    offenders = _offending_sites()
    assert offenders == {}, (
        "Inline UUID-shaped literal(s) found in cli_vcr assertions (issue #1452). "
        "Assertions must survive a re-record against a different notebook, so a "
        "value pinned from the recorded response is forbidden. Compare to a "
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


def test_detector_flags_pinned_uuid_in_assert() -> None:
    """The detector flags an inline UUID literal in any value-comparing assertion.

    Covers a bare ``assert ==`` (UUID on either side), and the ``assert*``
    unittest helpers beyond ``assertEqual`` — ``assertIn`` / ``assertDictEqual``
    must NOT bypass the gate (the broadened-method case from PR #1460 review).
    """
    src = "\n".join(
        [
            "assert data['notebook_id'] == 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'",  # 1
            "assert 'C3F6285F-1709-44C4-9CD6-E95CF0EA4F5E' == data['id']",  # 2 (uppercase)
            "self.assertEqual(out['id'], 'fdfc8ac4-3237-4f2a-8a79-3e24297a7040')",  # 3
            "assert src['id'] in {'00000000-0000-0000-0000-000000000000'}",  # 4 (set member)
            "self.assertIn('11111111-1111-1111-1111-111111111111', ids)",  # 5 (assertIn)
            "self.assertDictEqual(d, {'id': '22222222-2222-2222-2222-222222222222'})",  # 6
        ]
    )
    assert _uuid_literal_lines(ast.parse(src)) == [1, 2, 3, 4, 5, 6]


def test_detector_ignores_re_record_safe_assertions() -> None:
    """Placeholder names, schema/enum literals, and non-assert UUIDs are NOT flagged.

    These are the re-record-safe shapes the gate must tolerate:

    * comparison to a ``_fixtures`` placeholder *name* (the input-echo case);
    * schema/enum string literals (``"pass"`` / ``"RATE_LIMITED"`` / ``"delete"``)
      and input-echo non-UUID literals (a language code, an email);
    * a UUID literal that is *not* inside an assertion (e.g. a command argument
      passed to ``runner.invoke`` or a module-level placeholder definition).
    """
    benign = "\n".join(
        [
            "assert data['notebook_id'] == MUTATION_NOTEBOOK_ID",  # input-echo (Name)
            "assert data['checks']['auth']['status'] == 'pass'",  # schema enum
            "assert data.get('code') == 'RATE_LIMITED'",  # error enum
            "assert data['action'] == 'delete'",  # command action
            "assert data.get('language') == 'en'",  # input-echo language code
            "assert data.get('added_user') == VCR_SHARE_EMAIL",  # input-echo (Name)
            "result = runner.invoke(cli, ['source', 'get', 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'])",
            "PLACEHOLDER_NOTEBOOK_ID = 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'",
            # A context-manager assert takes no asserted *value* — a UUID inside
            # the managed block is a command arg, not a pinned comparison.
            "with self.assertRaises(ValueError):\n"
            "    runner.invoke(cli, ['source', 'get', 'c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e'])",
        ]
    )
    assert _uuid_literal_lines(ast.parse(benign)) == []
