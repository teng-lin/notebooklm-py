"""Notebook-context CLI services for ``use``, ``status``, and ``auth logout``.

Extracted from :mod:`notebooklm.cli.session_cmd` in P3.T3 so the Click
handlers stay thin orchestrators around small plan + executor pairs:

* :func:`verify_and_set_notebook` — async; resolves a partial ID,
  hits ``client.notebooks.get`` for verification, persists the result
  to ``context.json``. Returns the resolved :class:`Notebook` so the
  handler can render text or JSON.
* :func:`read_status` — sync; reads the active notebook id, joins it
  with the per-context JSON, and returns a :class:`StatusReport`
  the handler renders.
* :func:`render_status` — sync; renders a :class:`StatusReport` to the
  configured console.
* :func:`run_logout` — sync; removes the resolved storage file + browser
  profile + cached context. The handler is just the Click wrapper.

The handler keeps the legacy ``_use_notebook_table`` symbol locally so
existing tests that read ``session_cmd._use_notebook_table`` keep working.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.table import Table

from ...paths import get_browser_profile_dir, get_context_path, get_path_info
from ..context import clear_context, get_current_notebook
from ..error_handler import exit_with_code
from ..rendering import console, json_output_response
from .auth_source import AUTH_JSON_ENV_NAME, AuthSource, has_env_auth_json

# Capture the original function references at module-import time. The
# ``_resolve_paths_helper`` precedence chain compares each lookup
# against these (not against the live ``notebooklm.paths`` attribute or
# the module-level names — both of which can be patched by tests). This
# way "this site is the patched version" is decided unambiguously.
_ORIGINAL_GET_BROWSER_PROFILE_DIR = get_browser_profile_dir
_ORIGINAL_GET_CONTEXT_PATH = get_context_path
_ORIGINAL_GET_PATH_INFO = get_path_info


def _capture_original_get_storage_path():
    """Capture ``get_storage_path`` at import time without importing it eagerly.

    The eager import would pull all of ``notebooklm.paths`` into this
    service module's namespace, which complicates the
    ``_resolve_paths_helper`` precedence (the module-level binding
    would then participate in the patch detection). Keep the import
    function-local and stash the captured reference here.
    """
    from ...paths import get_storage_path

    return get_storage_path


_ORIGINAL_GET_STORAGE_PATH = _capture_original_get_storage_path()
del _capture_original_get_storage_path


# Resolve path helpers via a test-aware precedence chain so patches at
# ``notebooklm.cli.session_cmd.<sym>``,
# ``notebooklm.cli.services.session_context.<sym>``, AND
# ``notebooklm.paths.<sym>`` all intercept the service-layer call.
#
# ``default`` is the import-time function reference closed over by the
# caller. Comparisons go against ``default`` (NOT against the live
# ``notebooklm.paths`` attribute) so a patch at the canonical source does
# not falsely flag a stale local binding as "patched" — rev-3 CodeRabbit
# feedback on #962.
#
# Precedence:
#   1. The service module's own attribute if patched.
#   2. ``session_cmd``'s binding if patched.
#   3. Live ``notebooklm.paths`` value (which may itself be patched).
def _resolve_paths_helper(name: str, default):
    """Resolve a paths-helper symbol via the test-aware precedence chain."""
    import sys as _sys

    from ... import paths as _paths_module

    service_mod = _sys.modules.get(__name__)
    if service_mod is not None:
        local = getattr(service_mod, name, default)
        if local is not default:
            return local
    session_cmd = _sys.modules.get("notebooklm.cli.session_cmd")
    if session_cmd is not None:
        from_session = getattr(session_cmd, name, default)
        if from_session is not default:
            return from_session
    return getattr(_paths_module, name, default)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import click

    from ...client import NotebookLMClient
    from ...types import Notebook

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ``use`` — verify + persist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UseNotebookResult:
    """Resolved notebook + the canonical id the user passed.

    The handler uses ``notebook`` for rendering and ``resolved_id`` to
    persist the context (and surface as ``active_notebook_id`` in the
    JSON envelope).
    """

    notebook: Notebook
    resolved_id: str


async def verify_and_set_notebook(
    client: NotebookLMClient,
    partial_id: str,
    *,
    json_output: bool,
    resolver: Callable[..., Awaitable[str]] | None = None,
) -> UseNotebookResult:
    """Verify a (possibly partial) notebook id by hitting the server, then return it.

    The handler is responsible for actually persisting the resolved id to
    ``context.json`` after this returns — that side effect lives at the
    Click layer because it depends on ``set_current_notebook`` (which
    itself reads the current ``--storage`` override via the Click context).

    Errors mirror the legacy contract from ``cli/session_cmd.py``:

    * :class:`click.ClickException` from
      :func:`notebooklm.cli.resolve.resolve_notebook_id` (partial-id
      ambiguity or "no match") propagates unchanged.
    * :class:`NotebookNotFoundError`, :class:`AuthError`, and any other
      exception bubble up to the handler's body-error handler so the
      same "fail closed; --force is the escape hatch" UX applies.

    Args:
        client: An opened :class:`NotebookLMClient` (caller owns the
            ``async with`` lifecycle).
        partial_id: The id-or-prefix the user passed to ``notebooklm use``.
        json_output: Forwarded to ``resolver`` so its "Matched: ..."
            partial-id diagnostic routes to stderr in JSON mode and
            stdout stays pure parseable JSON.
        resolver: Injected partial-id resolver. Defaults to
            :func:`notebooklm.cli.resolve.resolve_notebook_id`. The handler
            in :mod:`notebooklm.cli.session_cmd` passes its locally-bound
            ``resolve_notebook_id`` so the legacy
            ``patch("notebooklm.cli.session_cmd.resolve_notebook_id", ...)``
            test seam keeps working.
    """
    if resolver is None:
        from ..resolve import resolve_notebook_id

        _resolver: Callable[..., Awaitable[str]] = resolve_notebook_id
    else:
        _resolver = resolver

    resolved_id = await _resolver(client, partial_id, json_output=json_output)
    nb = await client.notebooks.get(resolved_id)
    return UseNotebookResult(notebook=nb, resolved_id=resolved_id)


# ---------------------------------------------------------------------------
# ``status`` — read + project
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusContext:
    """The context-file payload joined with the active notebook id.

    The handler renders this either as a Rich table or as a JSON
    envelope; both views are explicit fields on this dataclass so the
    renderer never has to re-read the file.
    """

    has_context: bool
    notebook_id: str | None = None
    title: str | None = None
    is_owner: bool | None = None
    created_at: str | None = None
    conversation_id: str | None = None
    payload_readable: bool = True


@dataclass(frozen=True)
class StatusReport:
    """Result of :func:`read_status` — context + optional paths + env note.

    Attributes:
        context: The resolved notebook-context view (always present).
        paths: ``get_path_info(...)`` output when ``--paths`` was passed,
            else ``None``.
        has_env_auth: ``True`` when env-supplied auth is active;
            used by the ``--paths`` renderer to print the inline-auth
            note.
    """

    context: StatusContext
    paths: dict[str, Any] | None = None
    has_env_auth: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


def read_status(ctx: click.Context | None, *, show_paths: bool = False) -> StatusReport:
    """Read ``context.json`` for the active ``--storage``/profile and project it.

    Pure read-only — never mutates the context file. Returns the joined
    view as a :class:`StatusReport` so the handler can render either text
    or JSON without re-doing the path resolution.

    Path resolution goes through :class:`AuthSource` so the same precedence
    chain ``status`` uses matches ``use`` / ``auth check`` / etc.
    """
    auth = AuthSource.from_click_context(ctx)
    storage_override = auth.storage_override
    # Route the lookups through ``session_cmd`` so tests that patch
    # ``notebooklm.cli.session_cmd.get_context_path`` /
    # ``notebooklm.cli.session_cmd.get_path_info`` keep working
    # byte-for-byte. ``_ORIGINAL_*`` (captured at module-import time
    # below) are the "default" references — passing the module-level
    # names directly would re-read them at call time, defeating the
    # patched-vs-not test inside ``_resolve_paths_helper``.
    _get_context_path = _resolve_paths_helper("get_context_path", _ORIGINAL_GET_CONTEXT_PATH)
    _get_path_info = _resolve_paths_helper("get_path_info", _ORIGINAL_GET_PATH_INFO)
    context_file = _get_context_path(storage_path=storage_override)
    notebook_id = get_current_notebook()

    paths: dict[str, Any] | None = None
    if show_paths:
        paths = _get_path_info(storage_path=storage_override)

    if notebook_id is None:
        return StatusReport(
            context=StatusContext(has_context=False),
            paths=paths,
            has_env_auth=has_env_auth_json(),
        )

    try:
        data = json.loads(context_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Status: context file %s unreadable: %s", context_file, exc)
        return StatusReport(
            context=StatusContext(
                has_context=True,
                notebook_id=notebook_id,
                payload_readable=False,
            ),
            paths=paths,
            has_env_auth=has_env_auth_json(),
        )

    return StatusReport(
        context=StatusContext(
            has_context=True,
            notebook_id=notebook_id,
            title=data.get("title"),
            is_owner=data.get("is_owner"),
            created_at=data.get("created_at"),
            conversation_id=data.get("conversation_id"),
        ),
        paths=paths,
        has_env_auth=has_env_auth_json(),
    )


# ---------------------------------------------------------------------------
# ``auth logout`` helpers (path resolution stays in the service)
# ---------------------------------------------------------------------------


def resolve_logout_storage_path(ctx: click.Context | None) -> Path:
    """Pick the auth file ``auth logout`` should remove.

    When ``--storage <path>`` is active, that path IS the auth file;
    otherwise fall back to the per-profile ``storage_state.json``. The
    same precedence applies to the diagnostic message the handler
    prints if the unlink fails.
    """
    # Avoid the env-var fast path: ``auth logout`` always operates on a
    # concrete on-disk file (or no-ops when the profile has none).
    auth = AuthSource.from_click_context(ctx)
    if auth.storage_override is not None:
        return auth.storage_override
    # Module-level ``_ORIGINAL_GET_STORAGE_PATH`` is defined below the
    # imports (set once at import time) so the patched-vs-default check
    # in ``_resolve_paths_helper`` works correctly.
    _get_storage_path = _resolve_paths_helper("get_storage_path", _ORIGINAL_GET_STORAGE_PATH)
    return _get_storage_path(profile=auth.profile)


def warn_env_auth_remains_after_logout() -> bool:
    """Return ``True`` if the handler should print the env-still-active note."""
    return has_env_auth_json()


# ---------------------------------------------------------------------------
# ``status`` renderer (kept here so the Click handler stays a thin one-liner)
# ---------------------------------------------------------------------------


def render_status(report: StatusReport, *, json_output: bool) -> None:
    """Render a :class:`StatusReport` to the configured console.

    Supports ``--paths`` (shows the resolved configuration paths) and
    ``--json`` (machine-readable envelope). Preserves the legacy
    contract from :mod:`notebooklm.cli.session_cmd`.
    """
    if report.paths is not None:
        # --paths flag was set; render the paths view and stop.
        if json_output:
            json_output_response({"paths": report.paths})
            return

        table = Table(title="Configuration Paths")
        table.add_column("File", style="dim")
        table.add_column("Path", style="cyan")
        table.add_column("Source", style="green")

        path_info = report.paths
        table.add_row(
            "Profile",
            path_info.get("profile", "default"),
            path_info.get("profile_source", ""),
        )
        table.add_row("Home Directory", path_info["home_dir"], path_info["home_source"])
        table.add_row("Profile Directory", path_info.get("profile_dir", ""), "")
        table.add_row("Storage State", path_info["storage_path"], "")
        table.add_row("Context", path_info["context_path"], "")
        table.add_row("Browser Profile", path_info["browser_profile_dir"], "")

        if report.has_env_auth:
            console.print(
                f"[yellow]Note: {AUTH_JSON_ENV_NAME} is set (inline auth active)[/yellow]\n"
            )

        console.print(table)
        return

    ctx_view = report.context

    if not ctx_view.has_context:
        if json_output:
            json_output_response({"has_context": False, "notebook": None, "conversation_id": None})
            return
        console.print(
            "[yellow]No notebook selected. Use 'notebooklm use <id>' to set one.[/yellow]"
        )
        return

    if not ctx_view.payload_readable:
        # Context file existed but couldn't be parsed; surface minimal info.
        if json_output:
            json_output_response(
                {
                    "has_context": True,
                    "notebook": {
                        "id": ctx_view.notebook_id,
                        "title": None,
                        "is_owner": None,
                    },
                    "conversation_id": None,
                }
            )
            return

        table = Table(title="Current Context")
        table.add_column("Property", style="dim")
        table.add_column("Value", style="cyan")
        table.add_row("Notebook ID", ctx_view.notebook_id or "")
        table.add_row("Title", "-")
        table.add_row("Ownership", "-")
        table.add_row("Created", "-")
        table.add_row("Conversation", "[dim]None[/dim]")
        console.print(table)
        return

    if json_output:
        json_output_response(
            {
                "has_context": True,
                "notebook": {
                    "id": ctx_view.notebook_id,
                    "title": ctx_view.title if ctx_view.title and ctx_view.title != "-" else None,
                    "is_owner": ctx_view.is_owner if ctx_view.is_owner is not None else True,
                },
                "conversation_id": ctx_view.conversation_id,
            }
        )
        return

    table = Table(title="Current Context")
    table.add_column("Property", style="dim")
    table.add_column("Value", style="cyan")

    table.add_row("Notebook ID", ctx_view.notebook_id or "")
    table.add_row("Title", str(ctx_view.title or "-"))
    is_owner = ctx_view.is_owner if ctx_view.is_owner is not None else True
    owner_status = "Owner" if is_owner else "Shared"
    table.add_row("Ownership", owner_status)
    table.add_row("Created", ctx_view.created_at or "-")
    if ctx_view.conversation_id:
        table.add_row("Conversation", ctx_view.conversation_id)
    else:
        table.add_row("Conversation", "[dim]None (will auto-select on next ask)[/dim]")
    console.print(table)


# ---------------------------------------------------------------------------
# ``auth logout`` executor
# ---------------------------------------------------------------------------


def run_logout(ctx: click.Context | None) -> None:
    """Execute ``auth logout`` end-to-end (no return; calls ``exit_with_code`` on errors).

    Removes the resolved storage file, the cached browser profile, and
    the per-context cache file. Prints the env-still-active note when
    env-supplied auth survives the logout. The order matters:

    1. Storage file (the credential itself).
    2. Browser profile (the persistent SSO cache).
    3. Context cache (notebook + account routing).

    Each step is independent — failing one prints a partial-success
    diagnostic and exits 1; succeeding steps before the failure still
    print as removed.
    """
    if warn_env_auth_remains_after_logout():
        console.print(
            f"[yellow]Note: {AUTH_JSON_ENV_NAME} is set — env-based auth will "
            "remain active after logout. Unset it to fully log out.[/yellow]"
        )

    storage_path = resolve_logout_storage_path(ctx)
    _get_browser_profile_dir = _resolve_paths_helper(
        "get_browser_profile_dir", _ORIGINAL_GET_BROWSER_PROFILE_DIR
    )
    browser_profile = _get_browser_profile_dir()

    removed_any = False

    if storage_path.exists():
        try:
            storage_path.unlink()
            removed_any = True
        except OSError as exc:
            logger.error("Failed to remove auth file %s: %s", storage_path, exc)
            console.print(
                f"[red]Cannot remove auth file: {exc}[/red]\n"
                "Close any running notebooklm commands and try again.\n"
                f"If the problem persists, manually delete: {storage_path}"
            )
            exit_with_code(1)

    if browser_profile.exists():
        try:
            shutil.rmtree(browser_profile)
            removed_any = True
        except OSError as exc:
            logger.error("Failed to remove browser profile %s: %s", browser_profile, exc)
            partial = (
                "[yellow]Note: Auth file was removed, but browser profile "
                "could not be deleted.[/yellow]\n"
                if removed_any
                else ""
            )
            console.print(
                f"{partial}"
                f"[red]Cannot remove browser profile: {exc}[/red]\n"
                "Close any open browser windows and try again.\n"
                f"If the problem persists, manually delete: {browser_profile}"
            )
            exit_with_code(1)

    # In the natural call path ``clear_context`` is self-contained
    # (``_clear_context_file`` in ``cli/context.py`` catches every OSError
    # and returns ``"unavailable"`` literally), but tests in
    # ``test_auth_subcommands.py::TestAuthLogoutCommand`` patch the symbol
    # with ``side_effect=OSError(...)`` to assert the diagnostic UX. The
    # ``try/except`` is therefore reachable via the test surface and must
    # stay — claude[bot]'s rev-1 nitpick on #962 was incorrect for this
    # site (the function is patched on the service module's namespace, not
    # called through the real implementation).
    try:
        if clear_context(clear_account=True):
            removed_any = True
    except OSError as exc:
        storage_override = AuthSource.from_click_context(ctx).storage_override
        context_file = get_context_path(storage_path=storage_override)
        logger.error("Failed to remove context file %s: %s", context_file, exc)
        console.print(
            f"[red]Cannot remove context file: {exc}[/red]\n"
            "Close any running notebooklm commands and try again.\n"
            f"If the problem persists, manually delete: {context_file}"
        )
        exit_with_code(1)

    if removed_any:
        console.print("[green]Logged out.[/green] Run 'notebooklm login' to sign in again.")
    else:
        console.print("[yellow]No active session found.[/yellow] Already logged out.")


__all__ = [
    "StatusContext",
    "StatusReport",
    "UseNotebookResult",
    "read_status",
    "render_status",
    "resolve_logout_storage_path",
    "run_logout",
    "verify_and_set_notebook",
    "warn_env_auth_remains_after_logout",
]
