"""Transport-neutral session-context business logic.

This is the Click-free core of ``cli/services/session_context.py``: it owns the
``use`` verify-and-resolve workflow, the ``status`` read+project computation, and
the ``auth logout`` filesystem-teardown executor. Every transport adapter (the
Click CLI today, the FastMCP server / future HTTP surface tomorrow) drives this
core and renders the typed result into its own surface + exit-code policy.

Three boundary-imposed seams are worth calling out:

* **The partial-notebook-id resolver is injected, never imported.**
  ``cli.resolve.resolve_notebook_id`` reaches into ``rich`` consoles for its
  "Matched: ..." diagnostic, so this module cannot import it without breaking
  the ``_app`` boundary. :func:`verify_and_set_notebook` takes a
  ``resolve_notebook_id`` callable; the CLI wrapper passes its own (read at call
  time so the historical ``monkeypatch`` seam keeps landing).
* **The path/context helpers are injected via bundles, never imported.** The
  ``status`` read needs ``get_context_path`` / ``get_current_notebook`` /
  ``get_path_info``; the ``logout`` teardown needs the resolved storage path,
  ``get_browser_profile_dir``, and the ``clear_context`` callable. These are
  passed in :class:`StatusInputs` / :class:`LogoutInputs` bundles (the
  :class:`~notebooklm._app.doctor.DoctorPaths` pattern) so this core never
  reaches into ``notebooklm.paths`` / ``cli.context`` and the CLI's
  ``patch("...session_context.get_context_path")`` /
  ``patch("...session_context.clear_context")`` test seams keep landing (the CLI
  reads the helpers off its own service-module namespace at call time and
  forwards them here).
* **All rendering + exit-code policy stays in the CLI.** This core returns typed
  dataclasses only; the Rich tables and the green/yellow success lines live in
  ``cli/_session_render.py``.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import Notebook

logger = logging.getLogger(__name__)

#: Resolves a (possibly partial) notebook id to its full id. The CLI adapter
#: injects ``cli.resolve.resolve_notebook_id``; it is read off the wrapper at
#: call time so the ``monkeypatch`` test seam keeps landing.
ResolveNotebookIdFn = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# ``use`` — verify + resolve
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UseNotebookResult:
    """Resolved notebook + the canonical id the user passed.

    The adapter uses ``notebook`` for rendering and ``resolved_id`` to persist
    the context (and surface as ``active_notebook_id`` in the JSON envelope).
    Persisting the resolved id is a CLI-side side effect (it writes the active
    notebook pointer through the Click-context-aware ``set_current_notebook``),
    so it stays in the command layer; this core only verifies and returns.
    """

    notebook: Notebook
    resolved_id: str


async def verify_and_set_notebook(
    client: NotebookLMClient,
    partial_id: str,
    *,
    json_output: bool,
    resolve_notebook_id: ResolveNotebookIdFn,
) -> UseNotebookResult:
    """Verify a (possibly partial) notebook id by hitting the server, then return it.

    ``resolve_notebook_id`` is injected so this core stays free of the
    ``rich``-coupled resolver and the CLI's ``monkeypatch`` seam keeps landing.
    ``json_output`` is forwarded so the resolver's "Matched: ..." diagnostic
    routes to stderr in JSON mode (keeping stdout pure parseable JSON).

    Errors mirror the legacy contract: the resolver's ambiguity / "no match"
    error, plus :class:`NotebookNotFoundError` / :class:`AuthError` / any other
    exception, all propagate to the adapter's body-error handler.
    """
    resolved_id = await resolve_notebook_id(client, partial_id, json_output=json_output)
    notebook = await client.notebooks.get(resolved_id)
    return UseNotebookResult(notebook=notebook, resolved_id=resolved_id)


# ---------------------------------------------------------------------------
# ``status`` — read + project
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusContext:
    """The context-file payload joined with the active notebook id.

    The adapter renders this either as a Rich table or as a JSON envelope; both
    views are explicit fields so the renderer never has to re-read the file.
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
        paths: ``get_path_info(...)`` output when ``--paths`` was passed, else
            ``None``.
        has_env_auth: ``True`` when env-supplied auth is active; used by the
            ``--paths`` renderer to print the inline-auth note.
    """

    context: StatusContext
    paths: dict[str, Any] | None = None
    has_env_auth: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatusInputs:
    """Injected inputs for :func:`read_status`.

    The CLI builds this from its own ``session_context``-namespace helpers (the
    path resolvers read at call time) plus the already-resolved auth-source
    values, so the ``patch("...session_context.get_context_path")`` /
    ``get_current_notebook`` / ``get_path_info`` seams land and the env-auth
    note matches the resolver every other auth-aware command uses.

    Attributes:
        context_path: Resolved ``context.json`` path to read.
        notebook_id: The active notebook id (``None`` when no context is set).
        path_info: ``get_path_info(...)`` output when ``--paths`` was requested,
            else ``None`` (the adapter resolves this eagerly so the neutral core
            never reaches into ``notebooklm.paths``).
        has_env_auth: ``True`` when env-supplied auth is active.
    """

    context_path: Path
    notebook_id: str | None
    path_info: dict[str, Any] | None
    has_env_auth: bool


def read_status(inputs: StatusInputs) -> StatusReport:
    """Read ``context.json`` and project it into a typed :class:`StatusReport`.

    Pure read-only — never mutates the context file. Returns the joined view so
    the adapter can render either text or JSON without re-doing path resolution.
    """
    if inputs.notebook_id is None:
        return StatusReport(
            context=StatusContext(has_context=False),
            paths=inputs.path_info,
            has_env_auth=inputs.has_env_auth,
        )

    try:
        data = json.loads(inputs.context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Status: context file %s unreadable: %s", inputs.context_path, exc)
        data = None

    # A non-dict root (valid JSON that is a list/scalar) is the same failure
    # class as corrupt JSON here — ``status`` must not crash on a malformed
    # context file, so route it to the same payload_readable=False report rather
    # than letting ``data.get(...)`` raise ``AttributeError``.
    if not isinstance(data, dict):
        if data is not None:
            logger.debug(
                "Status: context file %s root is %s, not an object; treating as unreadable",
                inputs.context_path,
                type(data).__name__,
            )
        return StatusReport(
            context=StatusContext(
                has_context=True,
                notebook_id=inputs.notebook_id,
                payload_readable=False,
            ),
            paths=inputs.path_info,
            has_env_auth=inputs.has_env_auth,
        )

    return StatusReport(
        context=StatusContext(
            has_context=True,
            notebook_id=inputs.notebook_id,
            title=data.get("title"),
            is_owner=data.get("is_owner"),
            created_at=data.get("created_at"),
            conversation_id=data.get("conversation_id"),
        ),
        paths=inputs.path_info,
        has_env_auth=inputs.has_env_auth,
    )


# ---------------------------------------------------------------------------
# ``auth logout`` typed-outcome contract + executor
# ---------------------------------------------------------------------------


#: Discriminator for :class:`LogoutFailure`. Names the step whose filesystem
#: operation raised an :class:`OSError`. The adapter renders a different
#: diagnostic per kind so the user knows which artifact is locked and which
#: manual fallback path to use.
LogoutFailureKind = Literal["storage", "browser_profile", "context"]


@dataclass(frozen=True)
class LogoutFailure:
    """Typed description of an :class:`OSError` during one logout step.

    Attributes:
        kind: Which step raised — ``"storage"`` (auth file unlink),
            ``"browser_profile"`` (browser profile rmtree), or ``"context"``
            (context.json removal via the injected ``clear_context``).
        path: The resolved filesystem path that could not be removed. The
            adapter echoes this back so the user knows what to delete manually.
        error_message: ``str(error)`` captured at the point of failure. Stored as
            a string (not the raw exception) so the typed outcome remains
            hashable and so the ``logger.error(..., type(e).__name__)`` redaction
            (G6) controls what reaches the logging pipeline.
        partial_storage_removed: Only meaningful for
            ``kind == "browser_profile"`` — ``True`` when the auth file was
            already removed before the rmtree failed, so the adapter can print
            the partial-success note.
    """

    kind: LogoutFailureKind
    path: Path
    error_message: str
    partial_storage_removed: bool = False


@dataclass(frozen=True)
class LogoutOutcome:
    """Typed result of an :func:`execute_logout` invocation.

    The adapter dispatches on ``failure`` to decide exit 0 (success) vs. 1
    (per-step OSError), and on ``removed_any`` to pick the "Logged out" vs. "No
    active session found" success message.

    Attributes:
        removed_any: ``True`` when at least one artifact (auth file, browser
            profile, or context cache) was actually removed.
        env_auth_remains: ``True`` when env-supplied auth is still active after
            this logout (file-based artifacts removed but the env var survives).
        failure: ``None`` on success; a :class:`LogoutFailure` when one of the
            three filesystem steps raised :class:`OSError`.
    """

    removed_any: bool
    env_auth_remains: bool
    failure: LogoutFailure | None = None


@dataclass(frozen=True)
class LogoutInputs:
    """Injected inputs for :func:`execute_logout`.

    The CLI builds this from its own ``session_context``-namespace helpers so
    the ``patch("...session_context.clear_context")`` /
    ``get_browser_profile_dir`` / ``get_storage_path`` /
    ``get_context_path`` seams keep landing.

    Attributes:
        storage_path: The auth file ``logout`` should remove.
        browser_profile_dir: The cached browser-profile directory to rmtree.
        clear_context: Callable that removes the per-context cache and returns
            whether anything was removed (the CLI passes its
            ``clear_context``-bound wrapper).
        context_path: **Lazy** resolver for the context-file path, surfaced in
            the diagnostic when ``clear_context`` raises. Resolved only on the
            context-failure branch — never on the success path — so the legacy
            timing is preserved (the pre-refactor service resolved
            ``get_context_path`` lazily inside the ``except OSError`` block,
            after the storage/browser steps). Keeping it lazy means a patched /
            raising ``get_context_path`` cannot abort logout before the
            storage/browser teardown runs.
        env_auth_remains: ``True`` when env-supplied auth survives this logout.
        rmtree: Recursive directory remover (the CLI passes ``shutil.rmtree``).
    """

    storage_path: Path
    browser_profile_dir: Path
    clear_context: Callable[[], bool]
    context_path: Callable[[], Path]
    env_auth_remains: bool
    rmtree: Callable[[Path], Any]


def execute_logout(inputs: LogoutInputs) -> LogoutOutcome:
    """Execute ``auth logout`` end-to-end as a pure-typed-outcome operation.

    Removes the resolved storage file, the cached browser profile, and the
    per-context cache file. Returns a :class:`LogoutOutcome` carrying whichever
    step (if any) raised an :class:`OSError`; the adapter owns all presentation
    and exit-code policy. The order matches the legacy implementation:

    1. Storage file (the credential itself).
    2. Browser profile (the persistent SSO cache).
    3. Context cache (notebook + account routing).

    Each step is independent — a failure short-circuits the rest of the pipeline
    and returns immediately. The :class:`LogoutOutcome` records whether prior
    steps had already removed artifacts so the adapter can print the
    partial-success note.

    Logging contract (G6 redaction): ``OSError`` exceptions are captured as
    ``type(e).__name__`` in the structured log — never the raw exception object,
    which can contain user paths. The user-facing message still receives
    ``str(error)`` via :class:`LogoutFailure` because that's what the diagnostic
    line on stderr requires.
    """
    removed_any = False

    if inputs.storage_path.exists():
        try:
            inputs.storage_path.unlink()
            removed_any = True
        except OSError as exc:
            logger.error(
                "Failed to remove auth file %s: %s", inputs.storage_path, type(exc).__name__
            )
            return LogoutOutcome(
                removed_any=removed_any,
                env_auth_remains=inputs.env_auth_remains,
                failure=LogoutFailure(
                    kind="storage",
                    path=inputs.storage_path,
                    error_message=str(exc),
                ),
            )

    if inputs.browser_profile_dir.exists():
        try:
            inputs.rmtree(inputs.browser_profile_dir)
            removed_any = True
        except OSError as exc:
            logger.error(
                "Failed to remove browser profile %s: %s",
                inputs.browser_profile_dir,
                type(exc).__name__,
            )
            return LogoutOutcome(
                removed_any=removed_any,
                env_auth_remains=inputs.env_auth_remains,
                failure=LogoutFailure(
                    kind="browser_profile",
                    path=inputs.browser_profile_dir,
                    error_message=str(exc),
                    partial_storage_removed=removed_any,
                ),
            )

    # In the natural call path ``clear_context`` is self-contained (it catches
    # every OSError internally), but tests patch the injected callable with
    # ``side_effect=OSError(...)`` to assert the diagnostic UX, so the
    # ``try/except`` is reachable via the test surface and must stay.
    try:
        if inputs.clear_context():
            removed_any = True
    except OSError as exc:
        # Resolve the diagnostic context path lazily — only here, on the
        # failure branch — so a patched / raising ``get_context_path`` cannot
        # abort logout before the storage/browser teardown (legacy timing).
        context_path = inputs.context_path()
        logger.error(
            "Failed to remove context file %s: %s",
            context_path,
            type(exc).__name__,
        )
        return LogoutOutcome(
            removed_any=removed_any,
            env_auth_remains=inputs.env_auth_remains,
            failure=LogoutFailure(
                kind="context",
                path=context_path,
                error_message=str(exc),
            ),
        )

    return LogoutOutcome(
        removed_any=removed_any,
        env_auth_remains=inputs.env_auth_remains,
        failure=None,
    )


__all__ = [
    "LogoutFailure",
    "LogoutFailureKind",
    "LogoutInputs",
    "LogoutOutcome",
    "ResolveNotebookIdFn",
    "StatusContext",
    "StatusInputs",
    "StatusReport",
    "UseNotebookResult",
    "execute_logout",
    "read_status",
    "verify_and_set_notebook",
]
