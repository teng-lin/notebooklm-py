"""Friendly rookiepy error messages.

Leaf module within the ``cli/services/login/`` package — depends only on
:mod:`notebooklm.cli.rendering` (``console``).
"""

from __future__ import annotations

from ...rendering import console


def _handle_rookiepy_error(e: Exception, browser_name: str) -> None:
    """Print a user-friendly error for rookiepy exceptions."""
    msg = str(e).lower()
    if "lock" in msg or "database" in msg:
        console.print(
            f"[red]Could not read {browser_name} cookies: browser database is locked.[/red]\n"
            "Close your browser and try again."
        )
    elif "permission" in msg or "access" in msg:
        console.print(
            f"[red]Permission denied reading {browser_name} cookies.[/red]\n"
            "You may need to grant Terminal/Python access to your browser profile directory."
        )
    elif "keychain" in msg or "decrypt" in msg:
        console.print(
            f"[red]Could not decrypt {browser_name} cookies.[/red]\n"
            "On macOS, allow Keychain access when prompted, or try a different browser."
        )
    else:
        console.print(f"[red]Failed to read cookies from {browser_name}:[/red] {e}")
