"""Unit tests for the Claude Desktop ``.mcpb`` / DXT bundle.

Lives at the tests root (NOT under ``tests/unit/mcp/``) because it imports
nothing from ``fastmcp`` — the desktop bundle is a standalone launcher that
shells out to ``uvx``, so these tests run unconditionally even without the
``mcp`` extra installed.

Two artifacts under ``desktop-extension/`` are validated:

* ``manifest.json`` — a Claude Desktop extension manifest. We assert it is
  valid JSON, carries the required top-level keys, and that the server block
  invokes the bundled ``run_server.py`` launcher via ``python3``.
* ``run_server.py`` — a resilient launcher. We load it by file path (it is not
  an installed module) and exercise its ``uvx`` locator + exec-argv builder
  against a stub ``uvx`` on a temp ``PATH``. We never actually exec.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

# This file lives at tests/unit/test_mcp_desktop_extension.py → repo root is 3 up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXT_DIR = _REPO_ROOT / "desktop-extension"
_MANIFEST = _EXT_DIR / "manifest.json"
_RUN_SERVER = _EXT_DIR / "run_server.py"


def _load_run_server() -> ModuleType:
    """Import ``desktop-extension/run_server.py`` by file path (not a package)."""
    spec = importlib.util.spec_from_file_location("nlm_run_server", _RUN_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# manifest.json
# --------------------------------------------------------------------------- #


def test_manifest_is_valid_json_with_required_keys() -> None:
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))

    # DXT/.mcpb required top-level keys (mcpb MANIFEST spec v0.3).
    for key in ("manifest_version", "name", "version", "description", "author", "server"):
        assert key in data, f"manifest.json missing required key: {key!r}"

    # author.name is the only REQUIRED author subfield in the spec.
    assert data["author"]["name"], "manifest author.name is required by the mcpb spec"

    # Current mcpb/DXT spec is 0.3
    # (https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md).
    assert data["manifest_version"] == "0.3"

    # Server block points at the bundled launcher, run under python3.
    server = data["server"]
    assert server["type"] == "python"
    assert server["entry_point"] == "run_server.py"
    mcp_config = server["mcp_config"]
    assert mcp_config["command"] == "python3"
    assert mcp_config["args"] == ["${__dirname}/run_server.py"]


def test_manifest_has_win32_command_override() -> None:
    """Windows often lacks ``python3``; a platform override must launch ``python``.

    Without this override ``mcp_config.command == "python3"`` fails to launch the
    bundled extension on Windows. The mcpb spec supports per-platform overrides
    under ``mcp_config.platform_overrides`` (key ``win32``).
    """
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    mcp_config = data["server"]["mcp_config"]
    overrides = mcp_config.get("platform_overrides")
    assert isinstance(overrides, dict), "mcp_config.platform_overrides must be present"
    assert overrides.get("win32", {}).get("command") == "python", (
        "Windows override must launch 'python' (python3 is frequently absent on Windows)"
    )


def test_manifest_describes_this_package_not_the_competitor() -> None:
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    # Name is OUR server, not the competitor's distribution.
    assert data["name"] == "notebooklm-mcp"
    blob = json.dumps(data).lower()
    assert "notebooklm-py" in blob
    assert "notebooklm-mcp-cli" not in blob


# --------------------------------------------------------------------------- #
# run_server.py — uvx locator + exec-argv builder
# --------------------------------------------------------------------------- #


def _make_stub_uvx(directory: Path, name: str | None = None) -> Path:
    # On Windows, shutil.which("uvx") only resolves a name carrying a PATHEXT
    # extension, and _candidate_uvx_paths() looks for "uvx.exe" — so the stub
    # must be a .exe there for find_uvx() to discover it. On POSIX it's "uvx".
    if name is None:
        name = "uvx.exe" if os.name == "nt" else "uvx"
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_find_uvx_uses_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``uvx`` on PATH is discovered first."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = _make_stub_uvx(bindir)
    monkeypatch.setenv("PATH", str(bindir))

    run_server = _load_run_server()
    found = run_server.find_uvx()
    assert found is not None
    assert Path(found).resolve() == stub.resolve()


def test_find_uvx_falls_back_to_local_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing on PATH, ~/.local/bin/uvx is found via the candidate list."""
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    stub = _make_stub_uvx(local_bin)

    # Empty PATH (a dir with no uvx) so shutil.which misses and we hit candidates.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    # Windows expanduser("~") reads USERPROFILE, not HOME — set both so the
    # candidate ~/.local/bin path resolves to the temp home on every OS.
    monkeypatch.setenv("USERPROFILE", str(home))

    run_server = _load_run_server()
    found = run_server.find_uvx()
    assert found is not None
    assert Path(found).resolve() == stub.resolve()


def test_find_uvx_returns_none_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No uvx anywhere → locator returns None."""
    home = tmp_path / "home"
    home.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))

    run_server = _load_run_server()
    monkeypatch.setattr(run_server.os.path, "isfile", lambda _path: False)
    monkeypatch.setattr(run_server.os, "access", lambda _path, _mode: False)
    assert run_server.find_uvx() is None


def test_build_command_forwards_argv() -> None:
    """The exec argv runs the right package + console script, forwarding argv."""
    run_server = _load_run_server()
    cmd = run_server.build_command("/usr/bin/uvx", ["--transport", "http"])
    assert cmd == [
        "/usr/bin/uvx",
        "--from",
        "notebooklm-py[mcp]",
        "notebooklm-mcp",
        "--transport",
        "http",
    ]


def test_build_command_no_extra_argv() -> None:
    run_server = _load_run_server()
    cmd = run_server.build_command("/opt/uvx", [])
    assert cmd == ["/opt/uvx", "--from", "notebooklm-py[mcp]", "notebooklm-mcp"]


def test_main_errors_to_stderr_when_uvx_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When uvx is missing, main() prints to STDERR (never stdout) and exits nonzero.

    STDERR-only is critical: stdout is the JSON-RPC channel for the MCP host.
    """
    home = tmp_path / "home"
    home.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.argv", ["run_server.py"])

    run_server = _load_run_server()
    monkeypatch.setattr(run_server, "find_uvx", lambda: None)
    with pytest.raises(SystemExit) as excinfo:
        run_server.main()
    assert excinfo.value.code != 0

    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout — JSON-RPC channel stays clean
    assert "uvx" in captured.err.lower()


def test_run_server_does_not_print_to_stdout_on_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() with a stub uvx execs without writing to stdout itself.

    We patch subprocess.run so nothing is actually executed; assert main exits
    with the stub's return code and emits nothing on stdout.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_stub_uvx(bindir)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr("sys.argv", ["run_server.py", "--profile", "x"])

    run_server = _load_run_server()

    captured_cmd: dict[str, object] = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        captured_cmd["stdin"] = kwargs.get("stdin")
        captured_cmd["stdout"] = kwargs.get("stdout")
        captured_cmd["stderr"] = kwargs.get("stderr")
        return _Result()

    monkeypatch.setattr(run_server.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as excinfo:
        run_server.main()
    assert excinfo.value.code == 0

    cmd = captured_cmd["cmd"]
    assert cmd[1:5] == ["--from", "notebooklm-py[mcp]", "notebooklm-mcp", "--profile"]
    # stdio is passed through cleanly (critical for JSON-RPC).
    import sys

    assert captured_cmd["stdin"] is sys.stdin
    assert captured_cmd["stdout"] is sys.stdout
    assert captured_cmd["stderr"] is sys.stderr

    assert capsys.readouterr().out == ""


def test_run_server_is_executable_and_has_shebang() -> None:
    """The launcher is a runnable script (shebang + exec bit on POSIX)."""
    first_line = _RUN_SERVER.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!") and "python3" in first_line
    if os.name == "posix":
        mode = _RUN_SERVER.stat().st_mode
        assert mode & stat.S_IXUSR, "run_server.py should be executable"
