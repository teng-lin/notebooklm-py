"""Tests for the shared server-bootstrap helpers (``notebooklm._serving``).

This is the single source for loopback classification + the non-loopback bind
guard used by BOTH the ``notebooklm-mcp`` and ``notebooklm-server`` entry points
(they drifted before #1769). The `::ffff:127.0.0.1` cases pin the IPv4-mapped-IPv6
fix that the two naive per-server copies previously lacked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from notebooklm import _serving


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),  # case-insensitive
        ("  localhost  ", True),  # surrounding whitespace tolerated
        ("::ffff:127.0.0.1", True),  # IPv4-mapped IPv6 loopback (the #1769 fix)
        ("::ffff:7f00:1", True),  # same, hex form
        ("0.0.0.0", False),
        ("::", False),
        ("203.0.113.5", False),
        ("example.com", False),  # non-IP hostname is never loopback
        ("", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert _serving.is_loopback(host) is expected


def test_addr_is_loopback_ipv4_mapped() -> None:
    # The primitive server/_auth._addr_is_loopback delegates to — the request-path
    # loopback check must agree with the bind-path one on the mapped form.
    assert _serving.addr_is_loopback("::ffff:127.0.0.1") is True
    assert _serving.addr_is_loopback("not-an-ip") is False


def test_check_bind_allowed_empty_host_hard_refuses_even_with_override() -> None:
    with pytest.raises(SystemExit, match="empty host"):
        _serving.check_bind_allowed(
            "  ", allow_external=True, what="the X", allow_env="X_ALLOW_EXTERNAL_BIND"
        )


def test_check_bind_allowed_loopback_ok() -> None:
    _serving.check_bind_allowed(
        "127.0.0.1", allow_external=False, what="the X", allow_env="X_ALLOW"
    )
    _serving.check_bind_allowed(
        "::ffff:127.0.0.1", allow_external=False, what="the X", allow_env="X_ALLOW"
    )


def test_check_bind_allowed_non_loopback_refused_names_the_allow_env() -> None:
    with pytest.raises(SystemExit, match="MY_OVERRIDE_ENV"):
        _serving.check_bind_allowed(
            "0.0.0.0", allow_external=False, what="the X", allow_env="MY_OVERRIDE_ENV"
        )


def test_check_bind_allowed_non_loopback_allowed_with_override() -> None:
    _serving.check_bind_allowed(
        "203.0.113.5", allow_external=True, what="the X", allow_env="X_ALLOW"
    )


def test_serving_imports_no_cli_click_rich() -> None:
    """The MCP stdio entry point imports this module; it must stay free of
    ``click``/``rich``/``cli`` (import surface + pristine stdout). The dir-scoped
    mcp/server boundary guards don't cover this top-level module, so assert directly."""
    src = Path(_serving.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = [
        m for m in imported if m.split(".")[0] in {"click", "rich"} or "cli" in m.split(".")
    ]
    assert not forbidden, f"_serving.py must not import click/rich/cli, found: {forbidden}"
