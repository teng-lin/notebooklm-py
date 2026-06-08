"""Notebook-context CLI adapter for ``use``, ``status``, and ``auth logout``.

The session-context **business logic** lives in :mod:`notebooklm._app.session`
(the ``use`` verify/resolve workflow, the ``status`` read+project computation,
and the ``auth logout`` filesystem-teardown executor + typed outcomes). This
module is the thin CLI adapter on top of it: it

* re-exports the neutral typed result classes under their historical service
  names so ``cli/_session_render.py`` and the unit suite keep importing them
  from here,
* builds the injected path/context bundles by reading its **own**
  ``session_context``-namespace helpers (``get_context_path`` /
  ``get_current_notebook`` / ``get_path_info`` / ``get_storage_path`` /
  ``get_browser_profile_dir``) at call time, so the historical
  ``patch("notebooklm.cli.services.session_context.<helper>")`` test seams keep
  landing, and
* threads the active :class:`AuthSource` precedence (the single SoT) into the
  same path resolution every other auth-aware command uses.

Presentation (Rich tables, ``console.print``) and exit-code policy live in
:mod:`notebooklm.cli._session_render` / :mod:`notebooklm.cli.session_cmd`; this
adapter returns the typed neutral values only.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from ..._app.session import (
    LogoutFailure,
    LogoutFailureKind,
    LogoutInputs,
    LogoutOutcome,
    ResolveNotebookIdFn,
    StatusContext,
    StatusInputs,
    StatusReport,
    UseNotebookResult,
)
from ..._app.session import execute_logout as _execute_logout_core
from ..._app.session import read_status as _read_status_core
from ..._app.session import verify_and_set_notebook as _verify_and_set_notebook_core

# Path helpers are imported onto THIS module's namespace so the historical
# ``patch("notebooklm.cli.services.session_context.<helper>")`` seams land — the
# injected-bundle builders below read them through the module globals at call
# time, never close over them at import.
from ...paths import (
    get_browser_profile_dir,
    get_context_path,
    get_path_info,
    get_storage_path,
)
from ..context import clear_context, get_current_notebook
from .auth_source import AuthSource, has_env_auth_json

if TYPE_CHECKING:
    from pathlib import Path

    import click

    from ...client import NotebookLMClient

__all__ = [
    "LogoutFailure",
    "LogoutFailureKind",
    "LogoutOutcome",
    "StatusContext",
    "StatusReport",
    "UseNotebookResult",
    "execute_logout",
    "read_status",
    "resolve_logout_storage_path",
    "verify_and_set_notebook",
    "warn_env_auth_remains_after_logout",
]


# ---------------------------------------------------------------------------
# ``use`` — verify + persist
# ---------------------------------------------------------------------------


async def verify_and_set_notebook(
    client: NotebookLMClient,
    partial_id: str,
    *,
    json_output: bool,
    resolver: ResolveNotebookIdFn | None = None,
) -> UseNotebookResult:
    """Verify a (possibly partial) notebook id by hitting the server, then return it.

    Thin adapter over :func:`notebooklm._app.session.verify_and_set_notebook`.
    The handler is responsible for actually persisting the resolved id to
    ``context.json`` after this returns — that side effect lives at the Click
    layer because it depends on ``set_current_notebook`` (which reads the live
    ``--storage`` override via the Click context).

    Args:
        client: An opened :class:`NotebookLMClient` (caller owns the
            ``async with`` lifecycle).
        partial_id: The id-or-prefix the user passed to ``notebooklm use``.
        json_output: Forwarded to the resolver so its "Matched: ..." partial-id
            diagnostic routes to stderr in JSON mode.
        resolver: Injected partial-id resolver. Defaults to
            :func:`notebooklm.cli.resolve.resolve_notebook_id`. The handler in
            :mod:`notebooklm.cli.session_cmd` passes its locally-bound
            ``resolve_notebook_id`` so the legacy
            ``patch("notebooklm.cli.session_cmd.resolve_notebook_id", ...)`` test
            seam keeps working.
    """
    if resolver is None:
        from ..resolve import resolve_notebook_id

        resolver = resolve_notebook_id

    return await _verify_and_set_notebook_core(
        client,
        partial_id,
        json_output=json_output,
        resolve_notebook_id=resolver,
    )


# ---------------------------------------------------------------------------
# ``status`` — read + project
# ---------------------------------------------------------------------------


def read_status(ctx: click.Context | None, *, show_paths: bool = False) -> StatusReport:
    """Read ``context.json`` for the active ``--storage``/profile and project it.

    Builds the :class:`~notebooklm._app.session.StatusInputs` bundle from this
    module's own path helpers (read at call time so the ``get_context_path`` /
    ``get_current_notebook`` / ``get_path_info`` seams land) and delegates the
    pure read+project to the neutral core. Path resolution goes through
    :class:`AuthSource` so the same precedence ``status`` uses matches ``use`` /
    ``auth check``.
    """
    auth = AuthSource.from_click_context(ctx)
    storage_override = auth.storage_override
    context_file = get_context_path(storage_path=storage_override)
    notebook_id = get_current_notebook()

    path_info = get_path_info(storage_path=storage_override) if show_paths else None

    return _read_status_core(
        StatusInputs(
            context_path=context_file,
            notebook_id=notebook_id,
            path_info=path_info,
            has_env_auth=has_env_auth_json(),
        )
    )


# ---------------------------------------------------------------------------
# ``auth logout`` helpers (path resolution stays in the adapter)
# ---------------------------------------------------------------------------


def resolve_logout_storage_path(ctx: click.Context | None) -> Path:
    """Pick the auth file ``auth logout`` should remove.

    When ``--storage <path>`` is active, that path IS the auth file; otherwise
    fall back to the per-profile ``storage_state.json``. The same precedence
    applies to the diagnostic message the handler prints if the unlink fails.
    """
    # Avoid the env-var fast path: ``auth logout`` always operates on a concrete
    # on-disk file (or no-ops when the profile has none).
    auth = AuthSource.from_click_context(ctx)
    if auth.storage_override is not None:
        return auth.storage_override
    return get_storage_path(profile=auth.profile)


def warn_env_auth_remains_after_logout() -> bool:
    """Return ``True`` if the handler should print the env-still-active note."""
    return has_env_auth_json()


def execute_logout(ctx: click.Context | None) -> LogoutOutcome:
    """Execute ``auth logout`` end-to-end as a pure-typed-outcome operation.

    Thin adapter over :func:`notebooklm._app.session.execute_logout`: resolves
    the storage path / browser-profile dir through this module's own helpers (so
    the ``get_storage_path`` / ``get_browser_profile_dir`` / ``clear_context``
    patch seams land) and injects ``shutil.rmtree`` plus a ``clear_context``-bound
    wrapper. The context-file path is passed as a **lazy** resolver so it is read
    only on the context-failure diagnostic branch — matching the pre-refactor
    timing, where a patched / raising ``get_context_path`` never aborts logout
    before the storage/browser teardown. The neutral core owns the step ordering
    and the OSError→:class:`LogoutFailure` mapping; the caller owns presentation
    and exit-code policy.
    """

    def _context_path() -> Path:
        storage_override = AuthSource.from_click_context(ctx).storage_override
        return get_context_path(storage_path=storage_override)

    return _execute_logout_core(
        LogoutInputs(
            storage_path=resolve_logout_storage_path(ctx),
            browser_profile_dir=get_browser_profile_dir(),
            clear_context=lambda: clear_context(clear_account=True),
            context_path=_context_path,
            env_auth_remains=warn_env_auth_remains_after_logout(),
            rmtree=shutil.rmtree,
        )
    )
