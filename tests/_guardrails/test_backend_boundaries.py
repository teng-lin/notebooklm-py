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
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

# Modules become members only after their concrete web implementation has
# split away. Empty in A0 by design; A4-A9 add one entry per namespace split.
BASE_MODULE_ALLOWLIST: frozenset[str] = frozenset()

# Public dataclass decoders remain compatibility shims after their bodies move
# to ``_web.rows``. Permission is function-granular so another method in the
# same module cannot acquire a web dependency unnoticed.
LAZY_WEB_IMPORT_ALLOWLIST = frozenset(
    {
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
        "notebooklm._artifact",
        "notebooklm._artifact.downloads",
        "notebooklm._artifact.generation",
        "notebooklm._artifact.listing",
        "notebooklm._artifact.polling",
        "notebooklm._artifacts",
        "notebooklm._source",
        "notebooklm._source.batch",
        "notebooklm._source.content",
        "notebooklm._source.listing",
        "notebooklm._sources",
        "notebooklm._chat",
        "notebooklm._chat.api",
        "notebooklm._chat.history",
        "notebooklm._chat.notes",
        "notebooklm._chat.wire",
        "notebooklm._mind_map",
        "notebooklm._mind_maps_api",
        "notebooklm._note_service",
        "notebooklm._notebooks",
        "notebooklm._notes",
        "notebooklm._research",
        "notebooklm.research",
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
        target_is_web = _is_module_or_child(direct.target, "notebooklm._web")
        target_is_mobile = _is_module_or_child(direct.target, "notebooklm._mobile")
        target_is_rpc = _is_module_or_child(direct.target, "notebooklm.rpc")
        target_is_protobuf = _is_module_or_child(direct.target, "google.protobuf")

        reason: str | None = None
        if importer_is_mobile and (target_is_web or target_is_rpc):
            reason = "mobile backends must not import the web/RPC backend"
        elif importer_is_web and (target_is_mobile or target_is_protobuf):
            reason = "web backends must not import mobile/protobuf code"
        elif direct.importer in base_modules and (target_is_web or target_is_rpc):
            reason = "backend-neutral bases must not import _web or rpc"
        elif target_is_web and not importer_is_web:
            lazy_edge = (direct.importer, direct.scope) in LAZY_WEB_IMPORT_ALLOWLIST
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


def test_backend_boundary_manifests_are_well_formed() -> None:
    assert not BASE_MODULE_ALLOWLIST, "A0 begins before any facade/base split"
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
    assert "backend-neutral bases must not import _web or rpc" in violations[0]
