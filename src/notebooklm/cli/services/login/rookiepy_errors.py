"""Friendly rookiepy error messages.

Pure helper: classifies a rookiepy ``OSError``/``RuntimeError`` into one
of four user-facing message shapes (locked DB, permission denied,
decryption failure, generic) and returns the Rich-markup message text.

Callers are responsible for emission (``console.print``) and exit policy
(``exit_with_code`` / typed-outcome return). Keeping this module a pure
message formatter is what lets it live under :data:`GUARDED_PATHS` in
the services-boundary test — no presentation reach-in, no exit policy.
"""

from __future__ import annotations


def _handle_rookiepy_error(e: Exception, browser_name: str) -> str:
    """Return a Rich-markup user-facing error message for a rookiepy exception.

    The returned string carries Rich markup so callers can hand it
    straight to ``console.print`` (text mode) or strip the markup for the
    JSON envelope ``message`` field. The helper itself emits nothing.
    """
    msg = str(e).lower()
    if "lock" in msg or "database" in msg:
        return (
            f"[red]Could not read {browser_name} cookies: browser database is locked.[/red]\n"
            "Close your browser and try again."
        )
    if "permission" in msg or "access" in msg:
        return (
            f"[red]Permission denied reading {browser_name} cookies.[/red]\n"
            "You may need to grant Terminal/Python access to your browser profile directory."
        )
    if "keychain" in msg or "decrypt" in msg:
        return (
            f"[red]Could not decrypt {browser_name} cookies.[/red]\n"
            "On macOS, allow Keychain access when prompted, or try a different browser."
        )
    return f"[red]Failed to read cookies from {browser_name}:[/red] {e}"
