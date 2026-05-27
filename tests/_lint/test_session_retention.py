"""Meta-lint: every ``Session`` method must be listed in the retention doc.

Pairs with [`docs/session-method-retention.md`](../../docs/session-method-retention.md).
AST-parses [`src/notebooklm/_session.py`](../../src/notebooklm/_session.py),
enumerates every method / property defined inside the ``Session`` class body,
and asserts each one appears in the retention table with a valid disposition.

Adding a new method to ``Session`` without a corresponding row in the doc
fails this lint at PR time — the retention doc is the single source of truth
for the post-Wave-11 ``Session`` shape.

Lint shape (modeled after :mod:`tests._lint.test_no_session_compat_bridges`
and :mod:`tests._lint.test_no_core_imports`):

* True AST parse — no regex against the source.
* Enumerates :class:`ast.FunctionDef` + :class:`ast.AsyncFunctionDef` nodes
  whose immediate parent is the ``Session`` class body. Properties
  (``@property``-decorated functions) are included; nested functions inside
  methods are NOT (the parent walk gates them out).
* The doc parser scans the inventory table for rows shaped
  ``| `method_name` | category | disposition |``. Method names appear
  inside the first backtick-pair of column one; the entire backtick token
  may also carry a ``(property)`` suffix (e.g. ``kernel`` (property)).
* The only valid disposition is ``retain — <reason>``. Wave 11c tightened
  the lint to its **final form** when the three sub-wave cluster
  deletions (11a + 11b + 11c) had all landed: the transitional
  ``delete in Wave 11 (<cluster>)`` disposition that Wave 10 introduced
  to schedule the cluster deletions is no longer accepted; the rows
  that carried it moved to the **Deleted** section at the bottom of
  the retention doc (which the parser scopes out).

The lint enumerates methods only — not instance attributes like
``Session._rate_limit_max_retries`` (those are assigned in ``__init__`` and
documented in the "Stage-A and Rule-4 attribute capture targets" section of
the retention doc for context).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SESSION_MODULE: Path = REPO_ROOT / "src" / "notebooklm" / "_session.py"
RETENTION_DOC: Path = REPO_ROOT / "docs" / "session-method-retention.md"

SESSION_CLASS_NAME: str = "Session"

# Disposition prefixes that the retention doc may use. Wave 11c tightened
# this to ``{"retain"}`` after the three sub-wave cluster-deletion PRs
# (11a, 11b, 11c) had all landed; the transitional ``delete in Wave 11``
# prefix that Wave 10 introduced to schedule the cluster deletions is
# no longer accepted (any row that still carries it is doc drift and
# must be moved to the **Deleted** section at the bottom of the
# retention doc, which the parser scopes out).
_RETAIN_PREFIX: str = "retain"
VALID_DISPOSITION_PREFIXES: tuple[str, ...] = (_RETAIN_PREFIX,)
# Recognised but **rejected** prefix — kept as a named constant so the
# self-coverage tests below can pin "Wave 11c rejects this" without
# accidentally widening the live ``VALID_DISPOSITION_PREFIXES`` tuple.
_RETIRED_DELETE_PREFIX: str = "delete in Wave 11"


def _enumerate_session_methods(source: str) -> list[str]:
    """Return the ordered list of method/property names defined on ``Session``.

    Includes ``@property``-decorated functions; excludes nested functions
    defined inside method bodies (the parent walk gates them out).
    """
    tree = ast.parse(source)
    # Iterate ``tree.body`` directly rather than ``ast.walk(tree)``:
    # ``Session`` is a top-level class in ``_session.py`` and the lint
    # contract requires the *outermost* same-named class, so the
    # shallow walk is more predictable and rejects an accidental nested
    # ``class Session: ...`` inside a function body. (gemini-code-assist
    # review on PR #1075.)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == SESSION_CLASS_NAME:
            return [
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    raise AssertionError(
        f"{SESSION_MODULE.relative_to(REPO_ROOT)} no longer defines a "
        f"{SESSION_CLASS_NAME!r} class — update this lint to point at the new "
        "lifecycle owner."
    )


# Matches an inventory row's first column:
#   | `method_name` | ...
#   | `method_name` (property) | ...
# Captures ``method_name``. The optional ``(property)`` suffix lives outside
# the backticks per the doc's column-one convention. Leading whitespace
# before the pipe is tolerated so a future markdownlint / Prettier pass
# that indents tables doesn't silently empty the inventory.
# (gemini-code-assist review on PR #1075.)
_ROW_FIRST_CELL = re.compile(
    r"^\s*\|\s*`(?P<name>[A-Za-z_][A-Za-z0-9_]*)`(?:\s*\(property\))?\s*\|"
)


INVENTORY_SECTION_HEADER: str = "## Inventory"


def _parse_retention_doc(text: str) -> dict[str, str]:
    """Return a ``{method_name: disposition_cell}`` mapping from the doc.

    Only rows under the ``## Inventory`` section are considered. The
    ``## Categories`` / ``## Dispositions`` glossary tables earlier in the
    file also use backtick-quoted identifiers in column 1, but those name
    *categories* / *dispositions* — not Session methods — and would otherwise
    be misread as stale rows.

    The header row and separator row (``|---|---|---|``) do not start with a
    backticked identifier so they are naturally skipped by the regex. The
    next ``##`` heading after the inventory ends the scan window so the
    "Stage-A and Rule-4 attribute capture targets" section and the
    "Deleted" section are also out of scope.
    """
    lines = text.splitlines()
    rows: dict[str, str] = {}
    in_inventory = False
    for line in lines:
        if line.startswith("## "):
            in_inventory = line.strip() == INVENTORY_SECTION_HEADER
            continue
        if not in_inventory:
            continue
        match = _ROW_FIRST_CELL.match(line)
        if match is None:
            continue
        # Split the row into cells on ``|``; first and last entries are the
        # empty strings surrounding the leading / trailing pipes.
        cells = [cell.strip() for cell in line.split("|")]
        # cells = ["", "`name`...", category, disposition, ""]  → expect 5
        if len(cells) < 4:
            continue
        name = match.group("name")
        disposition = cells[3]
        rows[name] = disposition
    return rows


def _disposition_is_valid(disposition: str) -> bool:
    """Return ``True`` iff ``disposition`` starts with a valid prefix.

    After Wave 11c the retain branch is the only accepted shape — every
    row in the live Inventory must read ``retain — <reason>``. The
    transitional ``delete in Wave 11 (<cluster>)`` form that Wave 10
    introduced is now rejected; any row that still carries it is doc
    drift and must be moved to the **Deleted** section at the bottom
    of the retention doc (which the inventory parser scopes out).
    """
    return disposition.startswith(VALID_DISPOSITION_PREFIXES)


# ---------------------------------------------------------------------------
# Cached scans — load files once per test run.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_methods() -> list[str]:
    source = SESSION_MODULE.read_text(encoding="utf-8")
    return _enumerate_session_methods(source)


@pytest.fixture(scope="module")
def retention_rows() -> dict[str, str]:
    text = RETENTION_DOC.read_text(encoding="utf-8")
    return _parse_retention_doc(text)


# ---------------------------------------------------------------------------
# Self-coverage — prove the helpers behave as advertised.
# ---------------------------------------------------------------------------


def test_enumerate_session_methods_finds_known_methods() -> None:
    """Sanity: ``__init__`` and ``rpc_call`` must always be on Session."""
    source = SESSION_MODULE.read_text(encoding="utf-8")
    names = _enumerate_session_methods(source)
    assert "__init__" in names, "Session must always define __init__."
    assert "rpc_call" in names, (
        "Session.rpc_call is the public-API forward and must remain. If this "
        "lint enumerates an empty list, the AST walk lost the class body — "
        "check that the Session class still parses."
    )


def test_enumerate_session_methods_skips_nested_functions() -> None:
    """A nested function inside a method body must NOT appear in the inventory."""
    source = (
        "class Session:\n"
        "    def outer(self):\n"
        "        def nested():\n"
        "            return 1\n"
        "        return nested()\n"
    )
    names = _enumerate_session_methods(source)
    assert names == ["outer"], f"Expected only ['outer'], got {names!r}"


def test_enumerate_session_methods_skips_other_classes() -> None:
    """Methods on sibling classes must NOT be enumerated."""
    source = "class Other:\n    def stray(self): ...\nclass Session:\n    def real(self): ...\n"
    assert _enumerate_session_methods(source) == ["real"]


def test_parse_retention_doc_extracts_known_rows() -> None:
    """Sanity: parsing the live doc returns at least ``rpc_call`` with a retain disposition."""
    text = RETENTION_DOC.read_text(encoding="utf-8")
    rows = _parse_retention_doc(text)
    assert "rpc_call" in rows, (
        "Retention doc must list `rpc_call`. If the row was removed, the "
        "doc has drifted from the retention contract."
    )
    assert rows["rpc_call"].startswith(_RETAIN_PREFIX), (
        f"`rpc_call` disposition must be `retain — ...`, got: {rows['rpc_call']!r}"
    )


def test_parse_retention_doc_handles_property_suffix() -> None:
    """The ``(property)`` suffix outside backticks must not block the row match.

    Uses a ``retain`` disposition (the only one the live lint accepts after
    Wave 11c). The parser is opaque to disposition contents — it only
    captures the first-cell method name — but the sample chosen here
    matches the post-Wave-11 vocabulary so a future reader doesn't
    mistake it for live ``delete in Wave 11`` usage.
    """
    text = (
        "## Inventory\n"
        "\n"
        "| Method | Category | Disposition |\n"
        "|---|---|---|\n"
        "| `kernel` (property) | Stage A accessor | retain — Stage A accessor |\n"
    )
    rows = _parse_retention_doc(text)
    assert rows == {"kernel": "retain — Stage A accessor"}


def test_parse_retention_doc_skips_glossary_tables() -> None:
    """Backticked tokens in the Categories / Dispositions glossary tables
    appear before the inventory section and MUST NOT be parsed as method rows.
    """
    text = (
        "## Categories\n"
        "\n"
        "| Category | Meaning |\n"
        "|---|---|\n"
        "| `constructor` | something |\n"
        "| `lifecycle` | something else |\n"
        "\n"
        "## Inventory\n"
        "\n"
        "| Method | Category | Disposition |\n"
        "|---|---|---|\n"
        "| `rpc_call` | public API forward | retain — pinned |\n"
        "\n"
        "## Deleted\n"
        "\n"
        "| `old_method` | compatibility forward | deleted in #999 |\n"
    )
    rows = _parse_retention_doc(text)
    assert rows == {"rpc_call": "retain — pinned"}, (
        "Glossary and Deleted sections must not contribute rows."
    )


def test_disposition_validator_accepts_known_shapes() -> None:
    """``retain — <reason>`` is the only accepted shape after Wave 11c."""
    assert _disposition_is_valid("retain — lifecycle")
    assert _disposition_is_valid("retain — Stage A accessor")


def test_disposition_validator_rejects_retired_delete_prefix() -> None:
    """Wave 11c retired ``delete in Wave 11 (<cluster>)`` — the lint must reject it.

    Pins the **final-form** invariant: any row that still carries the
    transitional Wave-10 disposition is doc drift left over from before
    the three cluster-deletion PRs (11a, 11b, 11c) landed, and must be
    moved to the **Deleted** section at the bottom of the retention doc.
    """
    assert not _disposition_is_valid(f"{_RETIRED_DELETE_PREFIX} (`drain-and-operation`)")
    assert not _disposition_is_valid(f"{_RETIRED_DELETE_PREFIX} (`metrics-and-kernel`)")
    assert not _disposition_is_valid(f"{_RETIRED_DELETE_PREFIX} (`transport-and-reqid`)")


def test_disposition_validator_rejects_unknown_prefix() -> None:
    assert not _disposition_is_valid("TODO — figure it out later")
    assert not _disposition_is_valid("")


# ---------------------------------------------------------------------------
# The actual contract.
# ---------------------------------------------------------------------------


def test_every_session_method_appears_in_retention_doc(
    session_methods: list[str],
    retention_rows: dict[str, str],
) -> None:
    """Every method on ``Session`` must have a row in the retention doc."""
    missing = [name for name in session_methods if name not in retention_rows]
    assert not missing, (
        "These Session methods are not listed in "
        f"{RETENTION_DOC.relative_to(REPO_ROOT)}:\n"
        + "\n".join(f"  - {name}" for name in missing)
        + "\n\nAdd a row for each per the doc's existing format. Categories: "
        "lifecycle | public API forward | middleware chain leaf | "
        "provider-closure capture target | Stage A accessor | "
        "lazy collaborator factory | RefreshAuthCore Protocol surface | "
        "compatibility forward."
    )


def test_every_listed_disposition_is_valid(retention_rows: dict[str, str]) -> None:
    """Every row's disposition must start with a recognised prefix.

    After Wave 11c the only accepted prefix is ``retain``. Rows that
    still carry the transitional Wave-10 ``delete in Wave 11 (<cluster>)``
    disposition are doc drift and must be moved to the **Deleted**
    section at the bottom of the retention doc (which the parser scopes
    out, so a properly-located row does not surface here).
    """
    invalid = {
        name: disposition
        for name, disposition in retention_rows.items()
        if not _disposition_is_valid(disposition)
    }
    assert not invalid, (
        f"These rows in {RETENTION_DOC.relative_to(REPO_ROOT)} carry an "
        f"unrecognised disposition. Use `retain — <reason>`; the "
        f"transitional `delete in Wave 11 (<cluster>)` disposition was "
        f"retired by Wave 11c — move the row to the **Deleted** section "
        f"at the bottom of the doc instead. Offenders:\n"
        + "\n".join(f"  - `{name}`: {disposition!r}" for name, disposition in invalid.items())
    )


def test_retention_doc_does_not_list_unknown_methods(
    session_methods: list[str],
    retention_rows: dict[str, str],
) -> None:
    """A row that names a non-existent Session method indicates doc drift.

    If a method was deleted in Wave 11 (or earlier), its row should move to
    the **Deleted** section at the bottom of the doc rather than survive in
    the live inventory table.
    """
    known = set(session_methods)
    stale = [name for name in retention_rows if name not in known]
    assert not stale, (
        f"These rows in {RETENTION_DOC.relative_to(REPO_ROOT)} reference "
        "Session methods that no longer exist. Move them to the Deleted "
        "section (with the deleting PR's SHA) or remove them:\n"
        + "\n".join(f"  - `{name}`" for name in stale)
    )
