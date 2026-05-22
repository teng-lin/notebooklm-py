"""Firefox-family cookie helpers (containers + container-aware extractor).

Bypasses rookiepy for Firefox Multi-Account Containers because rookiepy
0.5.6 doesn't filter on ``originAttributes`` and silently merges every
container's cookies (see issues #366 / #367). Uses the helpers in
:mod:`notebooklm.cli._firefox_containers` to talk to ``cookies.sqlite``
directly.

Imports from :mod:`.cookie_jar` (allowed-but-unused per the DAG;
firefox container reads return raw cookie dicts that the caller hands
back to ``_enumerate_one_jar``), :mod:`.rookiepy_errors` (friendly
error printer), and :mod:`.cookie_domains` (domain-list builder).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ...error_handler import exit_with_code
from ...rendering import console
from .cookie_domains import _build_google_cookie_domains
from .rookiepy_errors import _handle_rookiepy_error


def _firefox_containers_module() -> Any:
    import importlib

    return importlib.import_module("notebooklm.cli._firefox_containers")


def _read_firefox_container_cookies(
    container_spec: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load Google cookies from a specific Firefox Multi-Account Container.

    Bypasses rookiepy because rookiepy 0.5.6 does not filter on
    ``originAttributes`` and silently merges every container's cookies (see
    issue #366 / #367). We talk to ``cookies.sqlite`` directly via the
    helpers in :mod:`notebooklm.cli._firefox_containers`.

    Args:
        container_spec: The part after ``firefox::`` (e.g. ``"Work"`` or
            ``"none"`` for the no-container default).
        verbose: When False, suppress the progress line; used by
            ``auth inspect --json``.

    Returns:
        Rookiepy-shape cookie dicts (compatible with
        :func:`convert_rookiepy_cookies_to_storage_state`).

    Raises:
        SystemExit: With a friendly message on any failure (no Firefox
            installed, unknown container, locked DB, …).
    """
    firefox_containers = _firefox_containers_module()

    profile_path = firefox_containers.find_firefox_profile_path()
    if profile_path is None:
        console.print(
            "[red]Could not locate a Firefox profile.[/red]\n"
            "Looked for profiles.ini in the standard Firefox locations. "
            "If you have Firefox installed in a non-standard location, the "
            "container-aware extractor cannot find it. Drop the '::<container>' "
            "suffix to fall back to rookiepy's autodetection."
        )
        exit_with_code(1)

    try:
        container_id = firefox_containers.resolve_container_id(profile_path, container_spec)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        exit_with_code(1)

    if verbose:
        if container_id == "none":
            console.print("[yellow]Reading cookies from Firefox (no container)...[/yellow]")
        else:
            console.print(
                f"[yellow]Reading cookies from Firefox container "
                f"'{container_spec}' (userContextId={container_id})...[/yellow]"
            )

    domains = _build_google_cookie_domains(include_domains=include_domains)
    try:
        return firefox_containers.extract_firefox_container_cookies(
            profile_path, container_id, domains=domains
        )
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        exit_with_code(1)
    except (OSError, RuntimeError) as e:
        _handle_rookiepy_error(e, "firefox")
        exit_with_code(1)
    except sqlite3.DatabaseError as e:
        console.print(f"[red]Failed to read Firefox cookies database:[/red] {e}")
        exit_with_code(1)


def _maybe_warn_firefox_containers_in_use() -> None:
    """Emit a one-line warning when unscoped ``firefox`` is risky.

    Triggers when ``cookies.sqlite`` has at least one row whose
    ``originAttributes`` carries a ``userContextId=`` field — i.e. the user
    really stored cookies inside some container. Cookie-driven (not
    ``containers.json``-driven) so stock built-in containers count just the
    same as user-created ones; First-Party-Isolation cookies (which only
    carry ``firstPartyDomain=``) do not trigger.

    Any probe failure is swallowed inside ``has_container_cookies_in_use``.
    """
    firefox_containers = _firefox_containers_module()

    profile_path = firefox_containers.find_firefox_profile_path()
    if profile_path is None:
        return
    if firefox_containers.has_container_cookies_in_use(profile_path):
        console.print(
            "[yellow]Warning: this Firefox profile has cookies stored inside "
            "a Multi-Account Container, but '--browser-cookies firefox' "
            "merges every container into one jar. If your Google session "
            "lives inside a container, re-run with "
            "[cyan]--browser-cookies 'firefox::<container-name>'[/cyan] "
            "(or [cyan]'firefox::none'[/cyan] for the no-container "
            "default).[/yellow]"
        )
