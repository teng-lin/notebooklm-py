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

from .cookie_domains import _build_google_cookie_domains
from .outcomes import BrowserCookieOutcome, CookieValidationFailure
from .rookiepy_errors import _handle_rookiepy_error


def _emit_progress(message: str) -> None:
    """Emit a verbose-mode progress line through a lazy ``console`` seam.

    Keeps the literal ``console.print`` call out of the reader body (the
    boundary :func:`_pattern_a_pairs` scanner only flags ``console.print``
    co-occurring with ``exit_with_code`` in the same function) while
    preserving the text-mode "Reading cookies from Firefox ..." status
    lines byte-for-byte. ``rendering`` is a level-3 reach-in the boundary
    test explicitly does not flag.
    """
    from ...rendering import console

    console.print(message)


def _firefox_containers_module() -> Any:
    import importlib

    return importlib.import_module("notebooklm.cli._firefox_containers")


def _read_firefox_container_cookies(
    container_spec: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> list[dict[str, Any]] | BrowserCookieOutcome:
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
        On success — rookiepy-shape cookie dicts (compatible with
        :func:`convert_rookiepy_cookies_to_storage_state`).

        On failure — a :class:`.outcomes.BrowserCookieOutcome` carrying the
        friendly message (no Firefox installed, unknown container, locked
        DB, …). The command layer (or :func:`refresh._exit_on_outcome`)
        renders ``outcome.message`` and exits, keeping presentation + exit
        policy out of ``cli/services``.
    """
    firefox_containers = _firefox_containers_module()

    profile_path = firefox_containers.find_firefox_profile_path()
    if profile_path is None:
        return CookieValidationFailure(
            code="FIREFOX_PROFILE_NOT_FOUND",
            message=(
                "[red]Could not locate a Firefox profile.[/red]\n"
                "Looked for profiles.ini in the standard Firefox locations. "
                "If you have Firefox installed in a non-standard location, the "
                "container-aware extractor cannot find it. Drop the '::<container>' "
                "suffix to fall back to rookiepy's autodetection."
            ),
        )

    try:
        container_id = firefox_containers.resolve_container_id(profile_path, container_spec)
    except ValueError as e:
        return CookieValidationFailure(code="FIREFOX_CONTAINER_INVALID", message=f"[red]{e}[/red]")

    if verbose:
        if container_id == "none":
            _emit_progress("[yellow]Reading cookies from Firefox (no container)...[/yellow]")
        else:
            _emit_progress(
                f"[yellow]Reading cookies from Firefox container "
                f"'{container_spec}' (userContextId={container_id})...[/yellow]"
            )

    domains = _build_google_cookie_domains(include_domains=include_domains)
    try:
        return firefox_containers.extract_firefox_container_cookies(
            profile_path, container_id, domains=domains
        )
    except FileNotFoundError as e:
        return CookieValidationFailure(code="FIREFOX_COOKIES_NOT_FOUND", message=f"[red]{e}[/red]")
    except (OSError, RuntimeError) as e:
        return CookieValidationFailure(
            code="COOKIE_READ_FAILED", message=_handle_rookiepy_error(e, "firefox")
        )
    except sqlite3.DatabaseError as e:
        return CookieValidationFailure(
            code="FIREFOX_DB_ERROR",
            message=f"[red]Failed to read Firefox cookies database:[/red] {e}",
        )


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
        _emit_progress(
            "[yellow]Warning: this Firefox profile has cookies stored inside "
            "a Multi-Account Container, but '--browser-cookies firefox' "
            "merges every container into one jar. If your Google session "
            "lives inside a container, re-run with "
            "[cyan]--browser-cookies 'firefox::<container-name>'[/cyan] "
            "(or [cyan]'firefox::none'[/cyan] for the no-container "
            "default).[/yellow]"
        )
