#!/usr/bin/env python3
"""Prove a built base wheel works without the optional Playwright dependency."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

EXPECTED_BROWSER_FILES = {
    "__init__.py",
    "browser_capture.py",
    "browser_launch_errors.py",
    "headless_reauth.py",
    "navigation_errors.py",
    "oauth_token.py",
}


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, env=env)


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _venv_console(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "notebooklm.exe"
    return root / "bin" / "notebooklm"


def _inspect_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        browser_files = {
            Path(name).name
            for name in names
            if name.startswith("notebooklm/_browser/") and name.endswith(".py")
        }
        missing = EXPECTED_BROWSER_FILES - browser_files
        if missing:
            raise RuntimeError(f"wheel is missing _browser modules: {sorted(missing)}")

        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError(f"expected one wheel METADATA file, found {metadata_names}")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))

    requirements = metadata.get_all("Requires-Dist", [])
    playwright_requirements = [
        requirement
        for requirement in requirements
        if requirement.split(";", 1)[0].strip().lower().startswith("playwright")
    ]
    if not playwright_requirements:
        raise RuntimeError("wheel metadata has no Playwright browser-extra requirement")
    markers = [
        requirement.partition(";")[2].replace("'", '"').replace(" ", "")
        for requirement in playwright_requirements
    ]
    if any("extra==" not in marker for marker in markers):
        raise RuntimeError(
            f"Playwright has an unconditional requirement: {playwright_requirements}"
        )
    if not any('extra=="browser"' in marker for marker in markers):
        raise RuntimeError("wheel metadata does not expose Playwright through the browser extra")


def _check_base_install(wheel: Path, interpreter: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="notebooklm-base-wheel-") as directory:
        root = Path(directory) / "venv"
        _run([str(interpreter), "-m", "venv", str(root)])
        python = _venv_python(root)
        env = {**os.environ, "PYTHONNOUSERSITE": "1"}
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            env=env,
        )

        _run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, sys; "
                    "assert importlib.util.find_spec('playwright') is None; "
                    "import notebooklm, notebooklm.auth, notebooklm.client; "
                    "assert 'notebooklm._browser' not in sys.modules; "
                    "assert not any(n == 'playwright' or n.startswith('playwright.') "
                    "for n in sys.modules)"
                ),
            ],
            env=env,
        )
        _run(
            [
                str(python),
                "-c",
                (
                    "import sys, notebooklm.notebooklm_cli; "
                    "assert not any(n == 'playwright' or n.startswith('playwright.') "
                    "for n in sys.modules)"
                ),
            ],
            env=env,
        )
        _run([str(_venv_console(root)), "--help"], env=env)
        _run(
            [
                str(python),
                "-c",
                (
                    "import sys; from notebooklm import auth; "
                    "assert auth.browser_login_channels(); "
                    "assert not any(n == 'playwright' or n.startswith('playwright.') "
                    "for n in sys.modules)"
                ),
            ],
            env=env,
        )

        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"{wheel}[browser]",
            ],
            env=env,
        )
        _run([str(python), "-c", "import playwright"], env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    wheel = args.wheel.resolve()
    interpreter = args.python.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        parser.error(f"--wheel must name one built wheel: {wheel}")
    if not interpreter.is_file():
        parser.error(f"--python does not exist: {interpreter}")

    _inspect_wheel(wheel)
    _check_base_install(wheel, interpreter)
    print("base-wheel/no-extra and browser-extra smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
