"""Tier boundary for the semantic operation vocabulary (P10 invariant I6).

``OperationDef.tier`` splits the closed vocabulary in two. ``PRODUCT`` members
back a public client method; the ``PRIMITIVE`` members are the decomposition
leaves a semantic service sequences to run a product workflow — the nine P9.2
leaves (``docs/plan/2026-08-24-p9-composite-gate-table.md`` §5) plus the P10
R2.2 streamed-answer leaf and the P10 R3.2 source-registration leaf.

Two things must stay true, and neither is self-enforcing:

* **Facades never reference a primitive.** A leaf is a service's building
  block; a facade reaching one would re-create the composite-below-the-port
  seam P9.2 removed. The gate scans every module at or above the facade layer
  for the primitive definition symbols and enum members.
* **Product counts exclude primitives.** The leaves inflate ``len(Operation)``
  without adding a product feature, so any "how many operations" number that
  counts them is wrong.

The primitive set itself is pinned by name: it is a reviewed vocabulary
decision, and growing it must be a diff-visible act rather than a side effect
of adding a def.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from scripts._operation_catalog_specs import OPERATION_SPECS
from scripts.audit_operation_catalog import operation_tier, product_operations

import notebooklm._semantic.records as records_module
from notebooklm._operations import Operation, OperationDef, OperationTier

pytestmark = pytest.mark.repo_lint

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

#: Prose prefixes a reviewed spec uses to mark a leaf. The programme that
#: introduced the leaf is part of the sentence, so the set grows with each one.
PRIMITIVE_PROSE_PREFIXES = ("P9.2 primitive:", "P10 primitive:")

#: The decomposition leaves: the nine P9.2 members, verbatim from the gate
#: table's §5 vocabulary, plus P10 R2.2's streamed-answer leaf and R3.2's
#: source-registration leaf. Adding another is a vocabulary decision, not a
#: refactor.
REVIEWED_PRIMITIVE_OPERATIONS = frozenset(
    {
        Operation.ARTIFACT_CATALOG,
        Operation.ARTIFACT_PATCH_TITLE,
        Operation.CHAT_STREAM_ANSWER,
        Operation.LABEL_ALLOCATE,
        Operation.LABEL_MUTATE,
        Operation.MIND_MAP_GENERATE,
        Operation.NOTEBOOK_ALLOCATE,
        Operation.NOTEBOOK_PATCH,
        Operation.SHARING_MUTATE,
        Operation.SHARING_PATCH_VIEW_LEVEL,
        Operation.SOURCE_PATCH_TITLE,
        Operation.SOURCE_REGISTER,
    }
)

#: Modules at or above the facade layer: the public ``*API`` namespaces, the
#: client that assembles them, and the transport-neutral adapters above them.
#: Completeness is asserted, not assumed, by
#: ``test_facade_inventory_covers_every_public_namespace_module``.
FACADE_MODULES = frozenset(
    {
        "_artifacts.py",
        "_chat/api.py",
        "_collections.py",
        "_labels.py",
        "_mind_maps_api.py",
        "_notebooks.py",
        "_notes.py",
        "_research.py",
        "_settings.py",
        "_sharing.py",
        "_sources.py",
        "client.py",
    }
)

#: Packages whose modules are all above the facade layer.
ABOVE_FACADE_PACKAGES = ("_app", "cli", "mcp", "server")


def primitive_definition_names() -> frozenset[str]:
    """Symbol names of every ``PRIMITIVE`` def, read from the live re-exports.

    ``_records`` re-exports the whole typed definition surface, so this derives
    the watch list from the defs themselves instead of copying nine names.
    """
    return frozenset(
        name
        for name, value in vars(records_module).items()
        if isinstance(value, OperationDef) and value.tier is OperationTier.PRIMITIVE
    )


def _watched_names() -> frozenset[str]:
    """Every identifier that names a primitive: its def symbol and enum member."""
    return primitive_definition_names() | {
        operation.name for operation in REVIEWED_PRIMITIVE_OPERATIONS
    }


def _scanned_paths() -> list[Path]:
    paths = [SRC_ROOT / module for module in sorted(FACADE_MODULES)]
    for package in ABOVE_FACADE_PACKAGES:
        paths.extend(sorted((SRC_ROOT / package).rglob("*.py")))
    return paths


def collect_primitive_references(source: str, *, watched: frozenset[str]) -> set[str]:
    """Return the watched primitive identifiers ``source`` names anywhere.

    Bare names, attribute access (``Operation.LABEL_MUTATE``) and import
    aliases all count — a facade that binds a leaf under another name has still
    reached one.
    """
    referenced: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in watched:
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in watched:
            referenced.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            referenced.update(alias.name for alias in node.names if alias.name in watched)
    return referenced


def test_reviewed_primitive_set_matches_the_live_definition_tiers() -> None:
    """The pinned vocabulary and the defs that carry ``PRIMITIVE`` agree."""
    live = frozenset(
        operation for operation in Operation if operation_tier(operation) is OperationTier.PRIMITIVE
    )

    assert live == REVIEWED_PRIMITIVE_OPERATIONS
    assert len(primitive_definition_names()) == len(REVIEWED_PRIMITIVE_OPERATIONS) == 12


def test_product_operation_count_excludes_the_primitives() -> None:
    """99 vocabulary members are 87 product operations plus twelve leaves."""
    assert len(Operation) == 99
    assert len(product_operations()) == 87
    assert product_operations().isdisjoint(REVIEWED_PRIMITIVE_OPERATIONS)
    assert product_operations() | REVIEWED_PRIMITIVE_OPERATIONS == frozenset(Operation)


def test_reviewed_catalog_prose_marks_exactly_the_primitive_tier() -> None:
    """The hand-authored spec prose and the live tier are independent and agree."""
    prose_marked = frozenset(
        spec.operation
        for spec in OPERATION_SPECS
        # The slice that introduced the leaf names itself in the prose; the
        # marker is the ``<slice> primitive:`` prefix, not the P9.2 number.
        if spec.composite_behavior.startswith(PRIMITIVE_PROSE_PREFIXES)
    )

    assert prose_marked == REVIEWED_PRIMITIVE_OPERATIONS


def test_facades_and_the_layers_above_reference_no_primitive_definition() -> None:
    """A leaf belongs to a service; nothing at or above the facade may name one."""
    watched = _watched_names()
    offenders = {
        path.relative_to(SRC_ROOT).as_posix(): sorted(referenced)
        for path in _scanned_paths()
        if (
            referenced := collect_primitive_references(
                path.read_text(encoding="utf-8"), watched=watched
            )
        )
    }

    assert offenders == {}


def test_facade_inventory_covers_every_public_namespace_module() -> None:
    """``FACADE_MODULES`` is the live ``*API`` module set, not a stale copy."""
    discovered = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative.split("/")[0] in ABOVE_FACADE_PACKAGES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.ClassDef) and node.name.endswith("API") for node in tree.body):
            discovered.add(relative)

    assert discovered | {"client.py"} == FACADE_MODULES


def test_reference_detector_bites_on_every_reaching_form() -> None:
    """Self-test: bare name, attribute, and import alias are all detected."""
    watched = _watched_names()

    assert collect_primitive_references(
        "from ._records import LABEL_MUTATE_DEF\n", watched=watched
    ) == {"LABEL_MUTATE_DEF"}
    assert collect_primitive_references(
        "await backend.invoke(LABEL_MUTATE_DEF, value, deadline=None)\n", watched=watched
    ) == {"LABEL_MUTATE_DEF"}
    assert collect_primitive_references(
        "require_leaves(backend, Operation.SHARING_MUTATE)\n", watched=watched
    ) == {"SHARING_MUTATE"}
    assert (
        collect_primitive_references(
            "await backend.invoke(LABEL_LIST_DEF, Operation.LABEL_LIST, deadline=None)\n",
            watched=watched,
        )
        == set()
    )
