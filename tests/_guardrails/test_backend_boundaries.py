"""Direct-import boundaries for the web/mobile backend split.

This gate intentionally inspects direct AST imports rather than transitive
imports. Importing ``notebooklm.rpc.types`` executes ``rpc/__init__.py``,
which will eventually re-export web-wire names, so a transitive reachability
rule would reject legitimate imports of neutral domain enums.

The manifests are migration scaffolding, not wildcards:

* ``BASE_MODULE_ALLOWLIST`` starts empty and gains a facade module only when
  that module has become a transport-neutral abstract base.
* ``LAZY_WEB_IMPORT_ALLOWLIST`` permits only the named compatibility shim
  method to import ``_web`` locally.
* ``ALLOWED_WEB_IMPORTERS`` is the complete set of composition, lifecycle,
  re-export, and package-shim edges that may import ``_web`` directly.

Imports guarded by ``if TYPE_CHECKING:`` do not create runtime coupling and
are exempt. Every other import statement, including a function-local one, is
checked.
"""

from __future__ import annotations

import ast
import enum
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest

import notebooklm._types.enums as domain_enums

pytestmark = pytest.mark.repo_lint

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
NEUTRAL_IDEMPOTENCY_PATH = SRC_ROOT / "_idempotency.py"
WEB_POLICY_PATH = SRC_ROOT / "_web" / "policy.py"
WEB_NAMESPACE_SHIMS: dict[str, str] = {}
REMOVED_EMPTY_PACKAGE_SHELLS = (
    "_chat",
    "_collection",
    "_label",
    "_middleware",
    "_row_adapters",
)

DOMAIN_ENUM_NAMES = frozenset(
    name
    for name, value in vars(domain_enums).items()
    if isinstance(value, type)
    and value.__module__ == domain_enums.__name__
    and issubclass(value, enum.Enum)
)

# Modules become members only after their concrete web implementation has
# split away. A4-A9 add one entry per namespace split.
BASE_MODULE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "notebooklm._artifacts",
        "notebooklm._chat",
        "notebooklm._collections",
        "notebooklm._labels",
        "notebooklm._mind_maps_api",
        "notebooklm._notebooks",
        "notebooklm._research",
        "notebooklm._sources",
        "notebooklm._notes",
        "notebooklm._settings",
        "notebooklm._sharing",
    }
)

# Public dataclass decoders remain compatibility shims after their bodies move
# to ``_web.rows``. Permission is function-granular so another method in the
# same module cannot acquire a web dependency unnoticed.
LAZY_WEB_IMPORT_ALLOWLIST = frozenset(
    {
        ("notebooklm._artifacts", "__getattr__"),
        ("notebooklm._chat", "__getattr__"),
        ("notebooklm._types.artifacts", "Artifact.from_api_response"),
        ("notebooklm._types.artifacts", "Artifact.from_mind_map"),
        ("notebooklm._types.artifacts", "_extract_artifact_url"),
        ("notebooklm._types.artifacts", "_extract_audio_artifact_url"),
        ("notebooklm._types.artifacts", "_extract_infographic_artifact_url"),
        ("notebooklm._types.artifacts", "_extract_slide_deck_artifact_url"),
        ("notebooklm._types.artifacts", "_extract_video_artifact_url"),
        ("notebooklm._types.collections", "Collection.from_api_response"),
        ("notebooklm._types.labels", "Label.from_api_response"),
        ("notebooklm._types.notebooks", "Notebook.from_api_response"),
        ("notebooklm._types.sharing", "ShareStatus.from_api_response"),
        ("notebooklm._types.sharing", "SharedUser.from_api_response"),
        ("notebooklm._types.sources", "Source.from_api_response"),
        ("notebooklm._types.sources", "_extract_source_created_at"),
        ("notebooklm._types.sources", "_extract_source_url"),
    }
)

ALLOWED_WEB_IMPORTERS = frozenset(
    {
        "notebooklm.client",
        "notebooklm._client_assembly",
        "notebooklm._runtime.lifecycle",
        "notebooklm._runtime.init",
        "notebooklm.rpc",
        "notebooklm.rpc.types",
        "notebooklm._artifact",
        "notebooklm._source",
    }
)

TRANSPORT_NEUTRAL_PACKAGE_PREFIXES = (
    "notebooklm._types",
    "notebooklm._app",
    "notebooklm.cli",
    "notebooklm.mcp",
    "notebooklm.server",
)


@dataclass(frozen=True)
class _DirectImport:
    importer: str
    target: str
    scope: str | None
    lineno: int
    type_only: bool


def _is_module_or_child(module: str, parent: str) -> bool:
    return module == parent or module.startswith(f"{parent}.")


def _is_type_checking_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
        and node.attr == "TYPE_CHECKING"
    )


class _DirectImportVisitor(ast.NodeVisitor):
    def __init__(self, *, importer: str, package: str) -> None:
        self._importer = importer
        self._package = package
        self._scope: list[str] = []
        self._type_only = False
        self.imports: list[_DirectImport] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking_guard(node.test):
            self.generic_visit(node)
            return

        previous = self._type_only
        self._type_only = True
        for child in node.body:
            self.visit(child)
        self._type_only = previous
        for child in node.orelse:
            self.visit(child)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            module = importlib.util.resolve_name(f"{'.' * node.level}{module}", self._package)

        # Retain the imported name as well as the module. This catches child
        # module forms such as ``from notebooklm import _web`` and
        # ``from google import protobuf`` without having to special-case roots.
        for alias in node.names:
            self._record(f"{module}.{alias.name}", node.lineno)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _record(self, target: str, lineno: int) -> None:
        self.imports.append(
            _DirectImport(
                importer=self._importer,
                target=target,
                scope=".".join(self._scope) or None,
                lineno=lineno,
                type_only=self._type_only,
            )
        )


def _module_identity(path: Path) -> tuple[str, str]:
    relative = path.relative_to(SRC_ROOT)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(("notebooklm", *parts))
        return module, module

    module = ".".join(("notebooklm", *parts))
    return module, module.rpartition(".")[0]


def _scan_path(path: Path) -> list[_DirectImport]:
    module, package = _module_identity(path)
    visitor = _DirectImportVisitor(importer=module, package=package)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.imports


def _scan_source(source: str, *, importer: str, package: str) -> list[_DirectImport]:
    visitor = _DirectImportVisitor(importer=importer, package=package)
    visitor.visit(ast.parse(source))
    return visitor.imports


def _boundary_violations(
    imports: list[_DirectImport],
    *,
    base_modules: frozenset[str] = BASE_MODULE_ALLOWLIST,
) -> list[str]:
    violations: list[str] = []
    for direct in imports:
        if direct.type_only:
            continue

        importer_is_web = _is_module_or_child(direct.importer, "notebooklm._web")
        importer_is_mobile = _is_module_or_child(direct.importer, "notebooklm._mobile")
        importer_is_types = _is_module_or_child(direct.importer, "notebooklm._types")
        importer_is_rpc = _is_module_or_child(direct.importer, "notebooklm.rpc")
        target_is_web = _is_module_or_child(direct.target, "notebooklm._web")
        target_is_mobile = _is_module_or_child(direct.target, "notebooklm._mobile")
        target_is_rpc = _is_module_or_child(direct.target, "notebooklm.rpc")
        target_is_protobuf = _is_module_or_child(direct.target, "google.protobuf")
        lazy_edge = (direct.importer, direct.scope) in LAZY_WEB_IMPORT_ALLOWLIST
        target_is_rpc_enum = any(
            direct.target in {f"notebooklm.rpc.{name}", f"notebooklm.rpc.types.{name}"}
            for name in DOMAIN_ENUM_NAMES
        )

        reason: str | None = None
        if importer_is_mobile and (target_is_web or target_is_rpc):
            reason = "mobile backends must not import the web/RPC backend"
        elif importer_is_web and (target_is_mobile or target_is_protobuf):
            reason = "web backends must not import mobile/protobuf code"
        elif (
            not lazy_edge
            and direct.importer in base_modules
            and (target_is_web or target_is_mobile or target_is_rpc)
        ):
            reason = "backend-neutral bases must not import backend implementations or rpc"
        elif importer_is_types and target_is_rpc:
            reason = "neutral types must not import the RPC compatibility package"
        elif target_is_rpc_enum and not importer_is_rpc:
            reason = "first-party enum consumers must import the neutral canonical module"
        elif target_is_web and not importer_is_web:
            allowed_edge = direct.importer in ALLOWED_WEB_IMPORTERS
            neutral_importer = direct.importer in base_modules or any(
                _is_module_or_child(direct.importer, prefix)
                for prefix in TRANSPORT_NEUTRAL_PACKAGE_PREFIXES
            )
            if neutral_importer and not lazy_edge:
                reason = "transport-neutral code may import _web only in a named lazy shim"
            elif not lazy_edge and not allowed_edge:
                reason = "direct _web importer is absent from the allowed-edge list"

        if reason is not None:
            scope = f" ({direct.scope})" if direct.scope else ""
            violations.append(
                f"{direct.importer}:{direct.lineno}{scope} -> {direct.target}: {reason}"
            )
    return violations


def _defined_scopes(path: Path) -> frozenset[str]:
    scopes: set[str] = set()
    stack: list[str] = []

    class ScopeVisitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            scopes.add(".".join(stack))
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    ScopeVisitor().visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return frozenset(scopes)


def test_backend_direct_import_boundaries() -> None:
    imports = [direct for path in sorted(SRC_ROOT.rglob("*.py")) for direct in _scan_path(path)]
    assert _boundary_violations(imports) == []


def test_idempotency_policy_has_one_web_owner_and_one_registry_seed() -> None:
    """A12 keeps create probing neutral and seeds web policy exactly once."""
    neutral_imports = _scan_path(NEUTRAL_IDEMPOTENCY_PATH)
    forbidden = [
        direct.target
        for direct in neutral_imports
        if _is_module_or_child(direct.target, "notebooklm._web")
        or _is_module_or_child(direct.target, "notebooklm.rpc")
    ]
    assert forbidden == []
    assert not (SRC_ROOT / "_idempotency_policy.py").exists()

    neutral_tree = ast.parse(NEUTRAL_IDEMPOTENCY_PATH.read_text(encoding="utf-8"))
    neutral_classes = {
        node.name for node in ast.walk(neutral_tree) if isinstance(node, ast.ClassDef)
    }
    assert neutral_classes.isdisjoint(
        {"IdempotencyEntry", "IdempotencyPolicy", "IdempotencyRegistry"}
    )

    policy_tree = ast.parse(WEB_POLICY_PATH.read_text(encoding="utf-8"))
    registry_assignments = [
        node
        for node in ast.walk(policy_tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "IDEMPOTENCY_REGISTRY"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    seed_calls = [
        node
        for node in ast.walk(policy_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_default_policies"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "IDEMPOTENCY_REGISTRY"
    ]
    assert len(registry_assignments) == 1
    assert len(seed_calls) == 1


@pytest.mark.parametrize(("filename", "implementation"), WEB_NAMESPACE_SHIMS.items())
def test_web_only_namespace_compatibility_modules_stay_thin_and_lazy(
    filename: str, implementation: str
) -> None:
    """A13 compatibility paths contain no facade body or eager web import."""
    path = SRC_ROOT / filename
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    functions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert functions == {"__dir__", "__getattr__"}
    assert not [
        direct
        for direct in _scan_path(path)
        if _is_module_or_child(direct.target, "notebooklm._web")
    ]

    implementation_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("notebooklm._web.")
    }
    assert implementation_literals == {implementation}


def test_relocated_package_shells_are_removed() -> None:
    """Completed moves leave no Python package source at the old paths."""
    stale = [
        str(path.relative_to(SRC_ROOT))
        for name in REMOVED_EMPTY_PACKAGE_SHELLS
        for path in (SRC_ROOT / name).rglob("*.py")
    ]
    assert stale == []


def test_backend_boundary_manifests_are_well_formed() -> None:
    assert (
        frozenset(
            {
                "notebooklm._artifacts",
                "notebooklm._chat",
                "notebooklm._collections",
                "notebooklm._labels",
                "notebooklm._mind_maps_api",
                "notebooklm._notebooks",
                "notebooklm._research",
                "notebooklm._notes",
                "notebooklm._settings",
                "notebooklm._sharing",
                "notebooklm._sources",
            }
        )
        == BASE_MODULE_ALLOWLIST
    )
    assert not BASE_MODULE_ALLOWLIST & ALLOWED_WEB_IMPORTERS

    for module, scope in LAZY_WEB_IMPORT_ALLOWLIST:
        relative = Path(*module.removeprefix("notebooklm.").split(".")).with_suffix(".py")
        path = SRC_ROOT / relative
        assert path.is_file(), f"lazy-shim module does not exist: {module}"
        assert scope in _defined_scopes(path), f"lazy-shim scope does not exist: {module}.{scope}"


def test_backend_boundary_matcher_handles_scope_relative_and_type_only_imports() -> None:
    imports = _scan_source(
        """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .._web.rows import ProjectRow
else:
    from .._mobile import codec

class Notebook:
    @classmethod
    def from_api_response(cls, row):
        from .._web import rows
        return rows.decode(row)
""",
        importer="notebooklm._types.notebooks",
        package="notebooklm._types",
    )

    assert [(item.target, item.scope, item.type_only) for item in imports] == [
        ("typing.TYPE_CHECKING", None, False),
        ("notebooklm._web.rows.ProjectRow", None, True),
        ("notebooklm._mobile.codec", None, False),
        ("notebooklm._web.rows", "Notebook.from_api_response", False),
    ]
    assert _boundary_violations(imports) == []


@pytest.mark.parametrize(
    ("importer", "source", "reason"),
    [
        (
            "notebooklm._mobile.notebooks",
            "from notebooklm.rpc import RPCMethod",
            "mobile backends must not import the web/RPC backend",
        ),
        (
            "notebooklm._web.notebooks",
            "from google.protobuf import message",
            "web backends must not import mobile/protobuf code",
        ),
        (
            "notebooklm._types.notebooks",
            "from .._web import rows",
            "transport-neutral code may import _web only in a named lazy shim",
        ),
        (
            "notebooklm._notebook_metadata",
            "from ._web import notebooks",
            "direct _web importer is absent from the allowed-edge list",
        ),
    ],
)
def test_backend_boundary_matcher_rejects_forbidden_edges(
    importer: str, source: str, reason: str
) -> None:
    package = importer.rpartition(".")[0]
    imports = _scan_source(source, importer=importer, package=package)
    assert reason in _boundary_violations(imports)[0]


def test_backend_boundary_matcher_rejects_rpc_import_from_enrolled_base() -> None:
    importer = "notebooklm._notebooks"
    imports = _scan_source(
        "from .rpc.types import RPCMethod",
        importer=importer,
        package="notebooklm",
    )

    violations = _boundary_violations(imports, base_modules=frozenset({importer}))
    assert "backend-neutral bases must not import backend implementations or rpc" in violations[0]


def test_backend_boundary_matcher_rejects_mobile_import_from_enrolled_base() -> None:
    importer = "notebooklm._notebooks"
    imports = _scan_source(
        "from ._mobile.notebooks import MobileNotebooksAPI",
        importer=importer,
        package="notebooklm",
    )

    violations = _boundary_violations(imports, base_modules=frozenset({importer}))
    assert "backend-neutral bases must not import backend implementations or rpc" in violations[0]


@pytest.mark.parametrize(
    "source",
    [
        "from ..rpc import safe_index as decode_at",
        "from ..rpc import RPCMethod as Method",
        "from .. import rpc as wire",
    ],
)
def test_backend_boundary_matcher_rejects_rpc_aliases_from_neutral_types(source: str) -> None:
    imports = _scan_source(
        source,
        importer="notebooklm._types.notebooks",
        package="notebooklm._types",
    )

    assert (
        "neutral types must not import the RPC compatibility package"
        in _boundary_violations(imports)[0]
    )


def test_backend_boundary_matcher_rejects_enum_compat_import_from_first_party_code() -> None:
    imports = _scan_source(
        "from ...rpc.types import ArtifactStatus as Status",
        importer="notebooklm._web.rows.artifacts",
        package="notebooklm._web.rows",
    )

    assert (
        "first-party enum consumers must import the neutral canonical module"
        in (_boundary_violations(imports)[0])
    )
