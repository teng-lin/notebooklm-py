#!/usr/bin/env python3
"""Repeatable live authentication matrix for maintainer/release validation.

This runner deliberately uses disposable ``NOTEBOOKLM_HOME`` directories for
every write.  It never logs cookie or token values.  The source profile and
browser profile are read-only inputs.

Example::

    uv run --extra browser --extra cookies --extra headless \
      python scripts/live_auth_matrix.py \
      --profile teng-lin-9420 \
      --browser 'chromium::Profile 3' \
      --account teng.lin.9420@gmail.com \
      --base-url https://notebooklm.google.com \
      --output live-matrix.json

The JSON report is suitable for attaching to a release checklist. All writes
go to a disposable ``NOTEBOOKLM_HOME`` and the temporary credential copies are
removed when the run finishes. Covered cells include baseline/live token
checks, browser-cookie login, master-token re-mint, cookie import filtering,
both NotebookLM hosts, concurrent refresh, true mid-session recovery,
transient-fault recovery (503, connection failure, and read timeout), and
crash-safe canonical writes.

The browser cookie extractor is host-sensitive: use the host where the browser
profile currently has its NotebookLM binding. Interactive Playwright login,
initial master-token bootstrap, CDP capture, Workspace/SSO, regional-account,
long-duration expiry, and MCP transport checks remain separate/manual cells.
MCP file-route coverage is available from ``scripts/mcp_live_smoke.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("NOTEBOOKLM_HOME", Path.home() / ".notebooklm"))


class Matrix:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results: list[dict[str, Any]] = []
        self.temp = Path(tempfile.mkdtemp(prefix="notebooklm-live-matrix-"))
        self.base_env = os.environ.copy()
        self.base_env.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "NOTEBOOKLM_BASE_URL": args.base_url,
                "NOTEBOOKLM_REFRESH_BROWSER": args.browser,
            }
        )
        self.base_env.pop("NOTEBOOKLM_AUTH_JSON", None)

    def env(self, home: Path, **extra: str) -> dict[str, str]:
        env = self.base_env.copy()
        env["NOTEBOOKLM_HOME"] = str(home)
        env.update(extra)
        return env

    def cli(
        self, home: Path, *args: str, profile: str | None = None, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "notebooklm.notebooklm_cli"]
        if profile:
            command.extend(["--profile", profile])
        command.extend(args)
        try:
            return subprocess.run(
                command,
                cwd=ROOT,
                env=self.env(home, **extra),
                text=True,
                capture_output=True,
                timeout=self.args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                command,
                124,
                exc.stdout or "",
                f"timed out after {self.args.timeout}s",
            )

    def record(
        self, name: str, proc: subprocess.CompletedProcess[str], *, expect_json: bool = False
    ) -> None:
        payload: dict[str, Any] = {
            "name": name,
            "status": "pass" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
        }
        if expect_json:
            try:
                payload["json"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload["json_error"] = proc.stdout[-1000:]
                payload["status"] = "fail"
        if proc.returncode != 0:
            payload["stderr"] = proc.stderr[-2000:]
        self.results.append(payload)

    def copy_profile(self, source: str, home: Path, target: str) -> None:
        source_dir = DEFAULT_HOME / "profiles" / source
        target_dir = home / "profiles" / target
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_dir, target_dir)

    def run(self) -> int:
        try:
            source_dir = DEFAULT_HOME / "profiles" / self.args.profile
            storage_path = source_dir / "storage_state.json"
            if not source_dir.is_dir() or not storage_path.is_file():
                self.results.append(
                    {
                        "name": "profile-input",
                        "status": "fail",
                        "error": f"profile or storage missing: {source_dir}",
                    }
                )
                return 1
            self.phase_baseline()
            if not self.args.skip_browser:
                self.phase_browser_discovery()
                self.phase_browser_login()
            self.phase_master_refresh()
            self.phase_import_filter()
            self.phase_hosts()
            self.phase_concurrency()
            if not self.args.skip_browser:
                self.phase_mid_session()
            self.phase_fault_injection()
            self.phase_crash_safety()
        finally:
            try:
                report = {
                    "revision": self.revision(),
                    "profile": self.args.profile,
                    "account": self.args.account,
                    "browser": self.args.browser,
                    "base_url": self.args.base_url,
                    "browser_cells": "skipped" if self.args.skip_browser else "executed",
                    **self.worktree_info(),
                    "results": self.results,
                    "temporary_home": str(self.temp),
                }
                output = json.dumps(report, indent=2, sort_keys=True)
                if self.args.output:
                    self.args.output.write_text(output + "\n", encoding="utf-8")
                print(output)
            finally:
                # Always remove the disposable home — it holds copied live
                # credentials, so a failure serializing/writing/printing the
                # report must never leave them on disk.
                shutil.rmtree(self.temp, ignore_errors=True)
        return 0 if all(item["status"] == "pass" for item in self.results) else 1

    def worktree_info(self) -> dict[str, Any]:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        ).stdout
        return {
            "worktree_dirty": bool(status),
            "worktree_diff_hash": hashlib.sha256(diff.encode()).hexdigest(),
        }

    def revision(self) -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False
        )
        return proc.stdout.strip()

    def phase_baseline(self) -> None:
        home = self.temp / "baseline"
        self.copy_profile(self.args.profile, home, "baseline")
        proc = self.cli(
            home,
            "auth",
            "check",
            "--test",
            "--passive",
            "--json",
            profile="baseline",
        )
        self.record("baseline-auth", proc, expect_json=True)
        proc = self.cli(home, "--quiet", "list", "--json", profile="baseline")
        self.record("baseline-list", proc, expect_json=True)

    def phase_browser_discovery(self) -> None:
        proc = self.cli(DEFAULT_HOME, "auth", "inspect", "--browser", self.args.browser, "--json")
        self.record("browser-account-discovery", proc, expect_json=True)

    def phase_browser_login(self) -> None:
        home = self.temp / "browser"
        proc = self.cli(
            home,
            "login",
            "--browser-cookies",
            self.args.browser,
            "--account",
            self.args.account,
            "--profile-name",
            "browser-test",
        )
        self.record("browser-cookie-login", proc)
        proc = self.cli(
            home, "auth", "check", "--test", "--passive", "--json", profile="browser-test"
        )
        self.record("browser-cookie-live-check", proc, expect_json=True)

    def phase_master_refresh(self) -> None:
        home = self.temp / "master"
        self.copy_profile(self.args.profile, home, "master-test")
        proc = self.cli(home, "login", "--master-token-refresh", profile="master-test")
        self.record("master-token-refresh", proc)
        proc = self.cli(
            home, "auth", "check", "--test", "--passive", "--json", profile="master-test"
        )
        self.record("master-token-live-check", proc, expect_json=True)

    def phase_import_filter(self) -> None:
        home = self.temp / "import"
        home.mkdir(parents=True)
        source = DEFAULT_HOME / "profiles" / self.args.profile / "storage_state.json"
        input_path = home / "input.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        data.setdefault("cookies", []).append(
            {
                "name": "MATRIX_SYNTHETIC_YOUTUBE",
                "value": "x",
                "domain": ".youtube.com",
                "path": "/",
            }
        )
        input_path.write_text(json.dumps(data), encoding="utf-8")
        storage = home / "storage_state.json"
        proc = self.cli(
            home, "--storage", str(storage), "auth", "import-cookies", str(input_path), "--json"
        )
        self.record("cookie-import-filter", proc, expect_json=True)
        if proc.returncode == 0:
            stored = json.loads(storage.read_text(encoding="utf-8"))
            ok = not any("youtube.com" in c.get("domain", "") for c in stored.get("cookies", []))
            self.results[-1]["filter_passed"] = ok
            if not ok:
                self.results[-1]["status"] = "fail"

    def phase_hosts(self) -> None:
        home = self.temp / "hosts"
        self.copy_profile(self.args.profile, home, "host-test")
        for host in ("https://notebook.google.com", "https://notebooklm.google.com"):
            proc = self.cli(
                home,
                "auth",
                "check",
                "--test",
                "--passive",
                "--json",
                profile="host-test",
                NOTEBOOKLM_BASE_URL=host,
            )
            self.record(f"host-{host.removeprefix('https://')}", proc, expect_json=True)

    def phase_concurrency(self) -> None:
        home = self.temp / "concurrency"
        self.copy_profile(self.args.profile, home, "shared")
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "notebooklm.notebooklm_cli",
                    "--profile",
                    "shared",
                    "auth",
                    "refresh",
                    "--verify",
                    "--json",
                ],
                cwd=ROOT,
                env=self.env(home),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        statuses: list[int] = []
        stderrs: list[str] = []
        for proc in processes:
            try:
                _out, err = proc.communicate(timeout=self.args.timeout)
                statuses.append(proc.returncode)
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, err = proc.communicate()
                statuses.append(-1)
            stderrs.append(err or "")
        entry: dict[str, Any] = {
            "name": "concurrent-refresh",
            "status": "pass" if statuses == [0] * 4 else "fail",
            "returncodes": statuses,
        }
        if entry["status"] == "fail":
            # Honor the report contract: attach bounded stderr tails for the
            # cells that failed. The CLI logs cookie names/diagnostics only,
            # never values, so a 2000-char tail carries no credential material.
            entry["stderr_tails"] = [
                err[-2000:] for status, err in zip(statuses, stderrs, strict=True) if status != 0
            ]
        self.results.append(entry)
        proc = self.cli(home, "auth", "check", "--test", "--passive", "--json", profile="shared")
        self.record("concurrent-refresh-final-check", proc, expect_json=True)

    def phase_mid_session(self) -> None:
        home = self.temp / "mid-session"
        self.copy_profile(self.args.profile, home, "mid-session")
        script = (
            "import asyncio\n"
            "from notebooklm import NotebookLMClient\n"
            "async def main():\n"
            "    async with NotebookLMClient.from_storage(profile='mid-session') as c:\n"
            "        before = await c.notebooks.list()\n"
            "        c._collaborators.kernel.get_http_client().cookies.clear()\n"
            "        after = await c.notebooks.list()\n"
            "        print(f'{len(before)} {len(after)} {len(c._collaborators.kernel.get_http_client().cookies)}')\n"
            "asyncio.run(main())\n"
        )
        command = [sys.executable, "-c", script]
        try:
            proc = subprocess.run(
                command,
                cwd=ROOT,
                env=self.env(
                    home,
                    NOTEBOOKLM_REFRESH_CMD=(
                        f"{shlex.quote(sys.executable)} "
                        f"{shlex.quote(str(ROOT / 'examples' / 'refresh_browser_cookies.py'))}"
                    ),
                    NOTEBOOKLM_REFRESH_CMD_MIDSESSION="1",
                ),
                text=True,
                capture_output=True,
                timeout=self.args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(
                command,
                124,
                exc.stdout or "",
                f"timed out after {self.args.timeout}s",
            )
        self.record("true-mid-session-recovery", proc)

    def phase_crash_safety(self) -> None:
        home = self.temp / "crash"
        home.mkdir(parents=True)
        source = DEFAULT_HOME / "profiles" / self.args.profile / "storage_state.json"
        expected = json.loads(source.read_text(encoding="utf-8"))
        expected_names = {
            cookie.get("name") for cookie in expected.get("cookies", []) if cookie.get("name")
        }
        required_names = {"SID", "APISID", "SAPISID", "LSID"}
        target = home / "storage_state.json"
        shutil.copy2(source, target)
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from notebooklm._auth.storage import replace_from_login\n"
            "p=Path(sys.argv[1]); state=json.loads(Path(sys.argv[2]).read_text())\n"
            "[replace_from_login(p, state, include_domains=None) for _ in range(10000)]\n"
        )
        passed = True
        for _ in range(3):
            proc = subprocess.Popen(
                [sys.executable, "-c", script, str(target), str(source)],
                cwd=ROOT,
                env=self.env(home),
            )
            time.sleep(0.08)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=self.args.timeout)
            try:
                state = json.loads(target.read_text(encoding="utf-8"))
                names = {
                    cookie.get("name") for cookie in state.get("cookies", []) if cookie.get("name")
                }
                passed = passed and required_names.issubset(names & expected_names)
            except json.JSONDecodeError:
                passed = False
        self.results.append(
            {
                "name": "crash-safe-storage-write",
                "status": "pass" if passed else "fail",
                "iterations": 3,
            }
        )

    def phase_fault_injection(self) -> None:
        # Isolate NOTEBOOKLM_HOME even for the pytest cell: base_env leaves it
        # unset, so a test that writes profile state could otherwise touch the
        # caller's real home, breaking the disposable-home guarantee.
        home = self.temp / "fault-injection"
        home.mkdir(parents=True, exist_ok=True)
        command = [
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/unit/test_error_injection_middleware.py",
            "tests/unit/test_retry_middleware.py",
            "tests/unit/test_auth_refresh_middleware.py",
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=ROOT,
                env=self.env(home),
                text=True,
                capture_output=True,
                timeout=self.args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(
                command,
                124,
                exc.stdout or "",
                f"timed out after {self.args.timeout}s",
            )
        self.record("transient-fault-injection-tests", proc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("NOTEBOOKLM_PROFILE", "default"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--browser", default="chromium::Profile 3")
    parser.add_argument("--base-url", default="https://notebook.google.com")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Skip rookiepy browser discovery/login cells when no fresh browser session is available.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(Matrix(parse_args()).run())
