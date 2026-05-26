"""Guard the post-consolidation RPC dependency vocabulary."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
UPLOAD_MODULE = Path("src/notebooklm/_source_upload.py")
RETIRED_RPC_CALLABLE_NAMES = frozenset({"RpcCall", "ShareRpc"})


def _source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _repo_relative(path: Path) -> Path:
    return path.resolve().relative_to(PROJECT_ROOT)


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]

    names: set[str] = set()
    for target in targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
    return names


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

    assert not offenders, (
        "Retired callable RPC dependency names must stay deleted; use "
        "`RpcCaller` for object-shaped feature RPC dependencies, or the "
        "upload-only `RpcCallback` keyword seam.\n\n" + "\n".join(offenders)
    )


def test_rpc_callback_stays_upload_only() -> None:
    offenders: list[str] = []
    for path in _source_files():
        relative = _repo_relative(path)
        if relative == UPLOAD_MODULE:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RpcCallback":
                offenders.append(f"{relative}:{node.lineno}: class RpcCallback")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and "RpcCallback" in _assigned_names(
                node
            ):
                offenders.append(f"{relative}:{node.lineno}: alias RpcCallback")

    assert not offenders, (
        "`RpcCallback` is reserved for SourceUploadPipeline.register_file_source's "
        "keyword-injected callback seam. Use `RpcCaller` for ordinary feature RPC "
        "dependencies.\n\n" + "\n".join(offenders)
    )
