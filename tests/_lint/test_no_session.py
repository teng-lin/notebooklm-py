"""Final guard that the deleted concrete session surface stays deleted."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
TEST_ROOT = REPO_ROOT / "tests"
SESSION_ATTR = "_" + "session"
DELETED_MODULE = "notebooklm" + "." + SESSION_ATTR
ALLOWED_MODULE_PREFIXES = (
    "notebooklm" + "." + SESSION_ATTR + "_helpers",
    "notebooklm" + "." + SESSION_ATTR + "_init",
)
OLD_HELPERS = {
    "build" + "_" + "session" + "_for_tests",
    "build_client" + "_for_tests",
}


def _python_files() -> list[Path]:
    return sorted(
        p
        for root in (SRC_ROOT, TEST_ROOT)
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _is_deleted_module_string(value: str) -> bool:
    if value in ALLOWED_MODULE_PREFIXES or any(
        value.startswith(prefix + ".") for prefix in ALLOWED_MODULE_PREFIXES
    ):
        return False
    return value == DELETED_MODULE or value.startswith(DELETED_MODULE + ".")


def _dynamic_attr_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name) and call.func.id in {"getattr", "setattr", "delattr"}:
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
            return call.args[1].value if isinstance(call.args[1].value, str) else None
    if isinstance(call.func, ast.Name) and call.func.id == "attrgetter":
        if call.args and isinstance(call.args[0], ast.Constant):
            return call.args[0].value if isinstance(call.args[0].value, str) else None
    if isinstance(call.func, ast.Attribute) and call.func.attr == "attrgetter":
        if call.args and isinstance(call.args[0], ast.Constant):
            return call.args[0].value if isinstance(call.args[0].value, str) else None
    return None


def _subscript_string_key(node: ast.Subscript) -> str | None:
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def scan_source(source: str, *, rel: str = "<memory>") -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Session":
            violations.append(f"{rel}:{node.lineno}: class Session")
        elif isinstance(node, ast.ImportFrom):
            if node.module in {SESSION_ATTR, DELETED_MODULE}:
                violations.append(f"{rel}:{node.lineno}: imports deleted module")
            if node.module == "notebooklm":
                for alias in node.names:
                    if alias.name in {SESSION_ATTR, DELETED_MODULE}:
                        violations.append(f"{rel}:{node.lineno}: imports deleted module")
            for alias in node.names:
                if alias.name in OLD_HELPERS:
                    violations.append(f"{rel}:{node.lineno}: imports old helper {alias.name}")
                if node.module in {SESSION_ATTR, DELETED_MODULE} and alias.name == "Session":
                    violations.append(f"{rel}:{node.lineno}: imports Session")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == DELETED_MODULE:
                    violations.append(f"{rel}:{node.lineno}: imports deleted module")
        elif isinstance(node, ast.Attribute):
            if node.attr == SESSION_ATTR:
                violations.append(f"{rel}:{node.lineno}: deleted session attribute")
            if node.attr == "collaborators":
                parent = getattr(node, "value", None)
                if isinstance(parent, ast.Name) and parent.id == "composed":
                    violations.append(f"{rel}:{node.lineno}: composed" + ".collaborators")
        elif isinstance(node, ast.Call):
            attr = _dynamic_attr_name(node)
            if attr == SESSION_ATTR:
                violations.append(f"{rel}:{node.lineno}: dynamic deleted session attr")
            if isinstance(node.func, ast.Name) and node.func.id in OLD_HELPERS:
                violations.append(f"{rel}:{node.lineno}: calls old helper {node.func.id}")
        elif isinstance(node, ast.Subscript):
            key = _subscript_string_key(node)
            if key == SESSION_ATTR:
                violations.append(f"{rel}:{node.lineno}: dynamic deleted session attr")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_deleted_module_string(node.value):
                violations.append(f"{rel}:{node.lineno}: string targets deleted module")
            old_helper_module = "_helpers" + "." + "session_factory"
            if node.value in OLD_HELPERS or old_helper_module in node.value:
                violations.append(f"{rel}:{node.lineno}: string mentions old helper")
    return violations


def test_no_deleted_session_surface_in_source_or_tests() -> None:
    violations: list[str] = []
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        violations.extend(scan_source(path.read_text(encoding="utf-8"), rel=rel))
    assert not violations, "Deleted session surface remains:\n  " + "\n  ".join(violations)


def test_scan_source_catches_static_and_dynamic_deleted_surface() -> None:
    source = "\n".join(
        [
            "class Session: pass",
            f"from notebooklm.{SESSION_ATTR} import Session",
            f"from notebooklm import {SESSION_ATTR}",
            f"import notebooklm.{SESSION_ATTR}",
            f"client.{SESSION_ATTR}.close()",
            f"getattr(client, {SESSION_ATTR!r})",
            f"setattr(client, {SESSION_ATTR!r}, None)",
            f"delattr(client, {SESSION_ATTR!r})",
            f"vars(client)[{SESSION_ATTR!r}]",
            f"client.__dict__[{SESSION_ATTR!r}]",
            f"attrgetter({SESSION_ATTR!r})",
            f"target = {DELETED_MODULE!r}",
            ("build_client" + "_for_tests(auth)"),
            "composed" + ".collaborators",
        ]
    )
    violations = scan_source(source)
    assert len(violations) >= 14


def test_scan_source_allows_surviving_sibling_modules() -> None:
    source = "\n".join(
        [
            repr("notebooklm" + "." + SESSION_ATTR + "_helpers.asyncio.sleep"),
            repr("notebooklm" + "." + SESSION_ATTR + "_init.httpx.AsyncClient"),
        ]
    )
    assert scan_source(source) == []
