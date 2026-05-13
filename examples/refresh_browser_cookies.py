#!/usr/bin/env python3
"""Default ``NOTEBOOKLM_REFRESH_CMD`` script: re-extract cookies from a live browser.

Wires the ``NOTEBOOKLM_REFRESH_CMD`` auto-refresh hook to the existing
``notebooklm login --browser-cookies`` flow so unattended automation can
recover from auth expiry without a human at the terminal.

When auth fails (e.g. session fully expired, force-logout, password changed,
or eventually a DBSC challenge that pure-httpx can't satisfy), the library
runs this script, reloads ``storage_state.json``, and retries the original
call once. The keepalive poke (``_poke_session``) handles routine SIDTS
rotation while you're active; this script handles the harder case of "the
session is gone, fetch a fresh one from a real browser."

Setup:
    pip install 'notebooklm-py[cookies]'    # rookiepy for cookie extraction
    export NOTEBOOKLM_REFRESH_CMD="python /absolute/path/refresh_browser_cookies.py"

    # Optional — pick a non-Chrome browser (chrome, edge, firefox, brave, ...)
    export NOTEBOOKLM_REFRESH_BROWSER=edge

The library injects ``NOTEBOOKLM_REFRESH_PROFILE`` and
``NOTEBOOKLM_REFRESH_STORAGE_PATH`` into this script's environment so it
targets the right profile / file.

Caveat:
    The source browser must already be logged into the same Google account.
    Cookie extraction reads whichever account is currently active in that
    browser — switch accounts in the browser before running if you need a
    different one for a given profile.
"""

import os
import subprocess
import sys


def main() -> int:
    missing = [
        var
        for var in ("NOTEBOOKLM_REFRESH_PROFILE", "NOTEBOOKLM_REFRESH_STORAGE_PATH")
        if not os.environ.get(var)
    ]
    if missing:
        print(
            "refresh_browser_cookies.py: missing required environment variable(s): "
            f"{', '.join(missing)}. This script is intended to be invoked by the "
            "library via NOTEBOOKLM_REFRESH_CMD, not run directly. See the docstring "
            "for setup instructions.",
            file=sys.stderr,
        )
        return 2

    profile = os.environ["NOTEBOOKLM_REFRESH_PROFILE"]
    storage = os.environ["NOTEBOOKLM_REFRESH_STORAGE_PATH"]
    browser = os.environ.get("NOTEBOOKLM_REFRESH_BROWSER", "chrome")

    # Use `python -m notebooklm.notebooklm_cli` instead of the `notebooklm` console
    # script so the script works inside venvs where the console script isn't on PATH
    # (e.g. cron jobs that don't source the venv's activate).
    # `--profile` is a top-level Click option and must come BEFORE the `login`
    # subcommand — Click rejects it after `login`.
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "notebooklm.notebooklm_cli",
            "--profile",
            profile,
            "login",
            "--browser-cookies",
            browser,
            "--storage",
            storage,
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
