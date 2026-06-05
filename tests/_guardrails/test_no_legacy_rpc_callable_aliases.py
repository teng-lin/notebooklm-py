"""Guard the post-consolidation RPC dependency vocabulary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
UPLOAD_MODULE = PROJECT_ROOT / "src" / "notebooklm" / "_source" / "upload.py"
RETIRED_RPC_CALLABLE_NAMES = frozenset({"RpcCall", "ShareRpc"})


def _source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _repo_relative(path: Path) -> Path:
    return path.resolve().relative_to(PROJECT_ROOT)


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]

    names: set[str] = set()

    def extract(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                extract(element)

    for target in targets:
        extract(target)

    return names


def _import_alias_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    names: set[str] = set()
    for alias in node.names:
        names.add(alias.name.rsplit(".", 1)[-1])
        if alias.asname:
            names.add(alias.asname)
    return names


def test_rpc_callable_guard_helpers_cover_unpacked_assignments_and_import_aliases() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "RpcCall, [ShareRpc] = callbacks",
                "from notebooklm._source.upload import RpcCallback as Callback",
                "import notebooklm._source.upload as RpcCallback",
            ]
        )
    )
    assign = tree.body[0]
    import_from = tree.body[1]
    import_node = tree.body[2]

    assert isinstance(assign, ast.Assign)
    assert _assigned_names(assign) == {"RpcCall", "ShareRpc"}

    assert isinstance(import_from, ast.ImportFrom)
    assert "RpcCallback" in _import_alias_names(import_from)

    assert isinstance(import_node, ast.Import)
    assert "RpcCallback" in _import_alias_names(import_node)


def test_retired_rpc_callable_names_do_not_return() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = _repo_relative(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in RETIRED_RPC_CALLABLE_NAMES:
                offenders.append(f"{relative}:{node.lineno}: class {node.name}")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for name in sorted(_assigned_names(node) & RETIRED_RPC_CALLABLE_NAMES):
                    offenders.append(f"{relative}:{node.lineno}: alias {name}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for name in sorted(_import_alias_names(node) & RETIRED_RPC_CALLABLE_NAMES):
                    offenders.append(f"{relative}:{node.lineno}: import {name}")

    assert not offenders, (
        "Retired callable RPC dependency names must stay deleted; use "
        "`RpcCaller` for object-shaped feature RPC dependencies, or the "
        "upload-only `RpcCallback` keyword seam.\n\n" + "\n".join(offenders)
    )


def test_rpc_callback_stays_upload_only() -> None:
    offenders: list[str] = []
    for path in _source_files():
        relative = _repo_relative(path)
        if path.resolve() == UPLOAD_MODULE:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RpcCallback":
                offenders.append(f"{relative}:{node.lineno}: class RpcCallback")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and "RpcCallback" in _assigned_names(
                node
            ):
                offenders.append(f"{relative}:{node.lineno}: alias RpcCallback")
            elif isinstance(
                node, (ast.Import, ast.ImportFrom)
            ) and "RpcCallback" in _import_alias_names(node):
                offenders.append(f"{relative}:{node.lineno}: import RpcCallback")

    assert not offenders, (
        "`RpcCallback` is reserved for SourceUploadPipeline.register_file_source's "
        "keyword-injected callback seam. Use `RpcCaller` for ordinary feature RPC "
        "dependencies.\n\n" + "\n".join(offenders)
    )
