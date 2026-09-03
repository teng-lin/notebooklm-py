"""Import-footprint guards for the optional browser implementation package."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from notebooklm.cli.services import playwright_login

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"


def _playwright_import_paths() -> set[str]:
    paths: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Import)
                and any(
                    alias.name == "playwright" or alias.name.startswith("playwright.")
                    for alias in node.names
                )
                or isinstance(node, ast.ImportFrom)
                and (node.module == "playwright" or (node.module or "").startswith("playwright."))
            ):
                paths.add(path.relative_to(SRC_ROOT).as_posix())
    return paths


def test_in_process_playwright_imports_live_only_in_browser_package() -> None:
    assert _playwright_import_paths() == {
        "_browser/browser_capture.py",
        "_browser/headless_reauth.py",
        "_browser/oauth_token.py",
    }


def test_chromium_probe_source_and_its_subprocess_consumer_are_exact() -> None:
    expected = f"""\
import os
import sys

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    path = playwright.chromium.executable_path
sys.stdout.write(
    "{playwright_login.CHROMIUM_PRESENT_MARKER}" if path and os.path.exists(path) else "{playwright_login.CHROMIUM_MISSING_MARKER}"
)
"""
    assert expected == playwright_login.CHROMIUM_PROBE_SOURCE

    tree = ast.parse(Path(playwright_login.__file__).read_text(encoding="utf-8"))
    consumers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "subprocess.run"
        and node.args
        and any(
            isinstance(item, ast.Name) and item.id == "CHROMIUM_PROBE_SOURCE"
            for item in ast.walk(node.args[0])
        )
    ]
    assert len(consumers) == 1


def test_base_import_does_not_load_browser_or_playwright() -> None:
    source = (
        "import sys\n"
        "import notebooklm, notebooklm.auth, notebooklm.client\n"
        "assert not any(n == 'notebooklm._browser' or n.startswith('notebooklm._browser.') "
        "for n in sys.modules)\n"
        "assert not any(n == 'playwright' or n.startswith('playwright.') for n in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cli_import_loads_no_playwright_driver() -> None:
    source = (
        "import sys\n"
        "import notebooklm.cli\n"
        "assert not any(n == 'playwright' or n.startswith('playwright.') for n in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
