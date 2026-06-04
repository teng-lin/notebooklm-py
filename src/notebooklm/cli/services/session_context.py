"""Notebook-context CLI services for ``use``, ``status``, and ``auth logout``.

The Click handlers stay thin orchestrators around small plan + executor pairs:

* :func:`verify_and_set_notebook` — async; resolves a partial ID,
  hits ``client.notebooks.get`` for verification, persists the result
  to ``context.json``. Returns the resolved :class:`Notebook` so the
  handler can render text or JSON.
* :func:`read_status` — sync; reads the active notebook id, joins it
  with the per-context JSON, and returns a :class:`StatusReport`
  the handler renders.
* :func:`execute_logout` — sync; removes the resolved storage file +
  browser profile + cached context. Returns a typed
  :class:`LogoutOutcome` capturing success or per-step failure; the
  handler owns the presentation and exit-code decisions (ADR-0015).

Presentation (Rich tables, ``console.print``) and exit-code policy live
in :mod:`notebooklm.cli.session_cmd`; this service module returns typed
values only.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ...paths import (
    get_browser_profile_dir,
    get_context_path,
    get_path_info,
    get_storage_path,
)
from ..context import clear_context, get_current_notebook
from .auth_source import AuthSource, has_env_auth_json

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
    context_file = get_context_path(storage_path=storage_override)
    notebook_id = get_current_notebook()

    paths: dict[str, Any] | None = None
    if show_paths:
        paths = get_path_info(storage_path=storage_override)

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
    return get_storage_path(profile=auth.profile)


def warn_env_auth_remains_after_logout() -> bool:
    """Return ``True`` if the handler should print the env-still-active note."""
    return has_env_auth_json()


# ---------------------------------------------------------------------------
# ``auth logout`` typed-outcome contract + executor
# ---------------------------------------------------------------------------


#: Discriminator for :class:`LogoutFailure`. Names the step whose
#: filesystem operation raised an :class:`OSError`. The handler renders a
#: different diagnostic per kind so the user knows which artifact is
#: locked and which manual fallback path to use.
LogoutFailureKind = Literal["storage", "browser_profile", "context"]


@dataclass(frozen=True)
class LogoutFailure:
    """Typed description of an :class:`OSError` during one logout step.

    Attributes:
        kind: Which step raised — ``"storage"`` (auth file unlink),
            ``"browser_profile"`` (browser profile rmtree), or
            ``"context"`` (context.json removal via
            :func:`notebooklm.cli.context.clear_context`).
        path: The resolved filesystem path that could not be removed.
            The handler echoes this back so the user knows what to
            delete manually.
        error_message: ``str(error)`` captured at the point of failure.
            Stored as a string (not the raw exception) so the typed
            outcome remains hashable and so the ``logger.error(...,
            type(e).__name__)`` redaction (G6) controls what reaches
            the logging pipeline.
        partial_storage_removed: Only meaningful for
            ``kind == "browser_profile"`` — ``True`` when the auth file
            was already removed before the rmtree failed, so the
            handler can print the "Auth file was removed, but browser
            profile could not be deleted" partial-success note.
    """

    kind: LogoutFailureKind
    path: Path
    error_message: str
    partial_storage_removed: bool = False


@dataclass(frozen=True)
class LogoutOutcome:
    """Typed result of an :func:`execute_logout` invocation.

    The handler in :mod:`notebooklm.cli.session_cmd` dispatches on
    ``failure`` to decide whether to exit 0 (success) or 1 (per-step
    OSError), and on ``removed_any`` to pick the "Logged out" vs.
    "No active session found" success message.

    Attributes:
        removed_any: ``True`` when at least one artifact (auth file,
            browser profile, or context cache) was actually removed.
            Drives the green "Logged out." vs. yellow "No active
            session found." success message.
        env_auth_remains: ``True`` when env-supplied auth is still
            active after this logout (file-based artifacts removed
            but the env var survives). Drives the env-still-active
            warning the handler prints BEFORE the success message.
        failure: ``None`` on success; a :class:`LogoutFailure` when
            one of the three filesystem steps raised :class:`OSError`.
    """

    removed_any: bool
    env_auth_remains: bool
    failure: LogoutFailure | None = None


def execute_logout(ctx: click.Context | None) -> LogoutOutcome:
    """Execute ``auth logout`` end-to-end as a pure-typed-outcome operation.

    Removes the resolved storage file, the cached browser profile, and
    the per-context cache file. Returns a :class:`LogoutOutcome` carrying
    whichever step (if any) raised an :class:`OSError`; the caller is
    responsible for all presentation and exit-code policy. The order
    matches the legacy implementation:

    1. Storage file (the credential itself).
    2. Browser profile (the persistent SSO cache).
    3. Context cache (notebook + account routing).

    Each step is independent — a failure short-circuits the rest of the
    pipeline and returns immediately. The :class:`LogoutOutcome` records
    whether prior steps had already removed artifacts so the handler can
    print the partial-success note.

    Logging contract (G6 redaction): ``OSError`` exceptions are captured
    as ``type(e).__name__`` in the structured log — never the raw
    exception object, which can contain user paths or other
    information not intended for the log sink. The user-facing message
    still receives ``str(error)`` via :class:`LogoutFailure` because
    that's what the diagnostic line on stderr requires.
    """
    env_auth_remains = warn_env_auth_remains_after_logout()

    storage_path = resolve_logout_storage_path(ctx)
    browser_profile = get_browser_profile_dir()

    removed_any = False

    if storage_path.exists():
        try:
            storage_path.unlink()
            removed_any = True
        except OSError as exc:
            logger.error("Failed to remove auth file %s: %s", storage_path, type(exc).__name__)
            return LogoutOutcome(
                removed_any=removed_any,
                env_auth_remains=env_auth_remains,
                failure=LogoutFailure(
                    kind="storage",
                    path=storage_path,
                    error_message=str(exc),
                ),
            )

    if browser_profile.exists():
        try:
            shutil.rmtree(browser_profile)
            removed_any = True
        except OSError as exc:
            logger.error(
                "Failed to remove browser profile %s: %s",
                browser_profile,
                type(exc).__name__,
            )
            return LogoutOutcome(
                removed_any=removed_any,
                env_auth_remains=env_auth_remains,
                failure=LogoutFailure(
                    kind="browser_profile",
                    path=browser_profile,
                    error_message=str(exc),
                    partial_storage_removed=removed_any,
                ),
            )

    # In the natural call path ``clear_context`` is self-contained
    # (``_clear_context_file`` in ``cli/context.py`` catches every OSError
    # and returns ``"unavailable"`` literally), but tests in
    # ``test_auth_subcommands.py::TestAuthLogoutCommand`` patch the symbol
    # with ``side_effect=OSError(...)`` to assert the diagnostic UX. The
    # ``try/except`` is therefore reachable via the test surface and must
    # stay: the function is patched on the service module's namespace, not
    # called through the real implementation.
    try:
        if clear_context(clear_account=True):
            removed_any = True
    except OSError as exc:
        storage_override = AuthSource.from_click_context(ctx).storage_override
        context_file = get_context_path(storage_path=storage_override)
        logger.error(
            "Failed to remove context file %s: %s",
            context_file,
            type(exc).__name__,
        )
        return LogoutOutcome(
            removed_any=removed_any,
            env_auth_remains=env_auth_remains,
            failure=LogoutFailure(
                kind="context",
                path=context_file,
                error_message=str(exc),
            ),
        )

    return LogoutOutcome(
        removed_any=removed_any,
        env_auth_remains=env_auth_remains,
        failure=None,
    )


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
