"""Single-wire-contract gate for the ``RotateCookies`` POST.

Before this gate, the POST to ``accounts.google.com/RotateCookies`` was
independently assembled at FOUR sites — the async keepalive poke
(``_auth/keepalive.py``), the file-based inline PSIDTS recovery and the
in-memory browser-extraction recovery (both ``_auth/psidts_recovery.py``),
and the master-token mint's completing leg (``_auth/master_token.py``) —
each re-listing the URL/headers/body/redirect/timeout quintet, and one of
them (the mint leg) silently missing ``raise_for_status``. A change to
Google's rotation protocol, or a fix to the request policy, had four places
to land and nothing keeping them in sync — in the credential-rotation path,
where drift is a correctness/security risk, not a style nit.

The contract now lives ONLY in ``_auth/keepalive.py`` (the
``_ROTATE_POST_KWARGS`` block plus the ``_rotate_post`` /
``_rotate_post_sync`` pair); every other module composes policy around those
entry points. This gate keeps a fifth bespoke assembly from creeping back:

1. the URL literal may appear in exactly one ``src/`` module (keepalive);
2. the request-body sentinel literal likewise;
3. no ``.post(...)`` call outside keepalive may take ``KEEPALIVE_ROTATE_URL``
   as its target (constructing a *non-sending* ``httpx.Request`` for the
   routability computation in ``psidts_recovery._psidts_routes_to_rotate``
   remains allowed — the gate matches ``.post`` calls, not the constant).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
_WIRE_OWNER = _SRC_ROOT / "_auth" / "keepalive.py"

_ROTATE_URL_LITERAL = "https://accounts.google.com/RotateCookies"
_ROTATE_BODY_LITERAL = '[000,"-0000000000000000000"]'


def _iter_src_modules() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _files_with_constant(value: str) -> list[Path]:
    hits: list[Path] = []
    for path in _iter_src_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == value:
                hits.append(path)
                break
    return hits


def test_rotate_url_literal_defined_once() -> None:
    """The RotateCookies URL string exists only in the wire-owner module."""
    assert _files_with_constant(_ROTATE_URL_LITERAL) == [_WIRE_OWNER], (
        "The RotateCookies URL literal must appear only in _auth/keepalive.py "
        "(as KEEPALIVE_ROTATE_URL). Other modules reference the constant, "
        "never the string."
    )


def test_rotate_body_literal_defined_once() -> None:
    """The RotateCookies request-body sentinel exists only in the wire owner."""
    assert _files_with_constant(_ROTATE_BODY_LITERAL) == [_WIRE_OWNER], (
        "The RotateCookies body sentinel must appear only in _auth/keepalive.py "
        "(as _KEEPALIVE_ROTATE_BODY)."
    )


def _references_rotate_url(node: ast.expr) -> bool:
    """True for ``KEEPALIVE_ROTATE_URL`` / ``<mod>.KEEPALIVE_ROTATE_URL``."""
    if isinstance(node, ast.Name):
        return node.id == "KEEPALIVE_ROTATE_URL"
    if isinstance(node, ast.Attribute):
        return node.attr == "KEEPALIVE_ROTATE_URL"
    return False


def test_no_bespoke_rotate_post_outside_wire_owner() -> None:
    """No ``.post(KEEPALIVE_ROTATE_URL, ...)`` call outside keepalive.

    Rotation callers go through ``_rotate_post`` / ``_rotate_post_sync`` so the
    full request policy (headers, body, redirects, timeout, raise_for_status)
    rides along; re-assembling the POST from the imported URL constant is
    exactly the drift this gate exists to stop.
    """
    violations: list[str] = []
    for path in _iter_src_modules():
        if path == _WIRE_OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "post"
                and node.args
                and _references_rotate_url(node.args[0])
            ):
                violations.append(f"{path.relative_to(_SRC_ROOT.parent.parent)}:{node.lineno}")
    assert not violations, (
        "Bespoke RotateCookies POST outside _auth/keepalive.py — route through "
        f"_rotate_post/_rotate_post_sync instead: {violations}"
    )
