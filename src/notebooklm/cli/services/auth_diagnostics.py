"""``auth check`` CLI adapter over the transport-neutral diagnostics core.

The ``auth check`` **business logic** (the storage-exists / JSON-valid /
cookies-present / SID / optional token-fetch probe) lives in
:mod:`notebooklm._app.auth_check`. This module is the thin CLI adapter on top of
it: it

* re-exports the neutral :class:`AuthCheckPlan` / :class:`AuthCheckResult` under
  their historical names so ``cli/_session_render.py`` and the unit suite keep
  importing them from here,
* builds the plan from the live Click context via the ``AuthSource`` precedence
  resolver (:func:`plan_from_click_context`), resolving the ``auth_source``
  display label here so the neutral core never branches on the env-var name, and
* injects the consolidated ``read_env_auth_json`` accessor into the neutral
  executor so the inline-auth read stays the single env-var SoT
  (``AUTH_JSON_ENV_NAME`` in ``cli.services.auth_source``).

Rendering and exit-code policy live in the command layer (see
``cli/_session_render.py`` / ``cli/session_cmd.py``).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..._app.auth_check import AuthCheckPlan, AuthCheckResult
from ..._app.auth_check import run_auth_check as _run_auth_check_core
from .auth_source import AUTH_JSON_ENV_NAME, AuthSource, read_env_auth_json

__all__ = [
    "AuthCheckPlan",
    "AuthCheckResult",
    "format_auth_source",
    "plan_from_click_context",
    "run_auth_check",
]


def _auth_source_label(*, has_env_auth: bool, has_home_env: bool, storage_path: Path) -> str:
    """Resolve the ``auth check`` ``auth_source`` display label from components.

    The single implementation of the label-formatting logic; both the
    plan-build path and the public :func:`format_auth_source` helper delegate
    here so the branch wording lives in one place.
    """
    if has_env_auth:
        return AUTH_JSON_ENV_NAME
    if has_home_env:
        return f"$NOTEBOOKLM_HOME ({storage_path})"
    return f"file ({storage_path})"


def format_auth_source(plan: AuthCheckPlan) -> str:
    """Human-readable description of where ``plan`` reads auth from.

    Public helper so the command-layer renderer can re-use the same string in
    the Rich table and the JSON ``details.auth_source`` field. Equivalent to the
    label already carried on ``plan.auth_source_label`` (resolved at plan-build
    time); kept as a function for the historical patch/import surface.
    """
    return _auth_source_label(
        has_env_auth=plan.has_env_auth,
        has_home_env=plan.has_home_env,
        storage_path=plan.storage_path,
    )


def plan_from_click_context(
    ctx, *, test_fetch: bool, json_output: bool, passive: bool = False
) -> AuthCheckPlan:
    """Build an :class:`AuthCheckPlan` from a Click context + flags.

    The profile + storage path come from the same :class:`AuthSource` resolver
    every other auth-aware command uses, so the diagnostic reports the same file
    the runtime would actually try to load. The ``auth_source`` display label is
    resolved here so the neutral core stays free of the env-var-name branch.
    """
    auth = AuthSource.from_click_context(ctx)
    storage_path = auth.storage_path_for_diagnostics()
    has_env_auth = auth.has_env_auth
    has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))
    return AuthCheckPlan(
        storage_path=storage_path,
        profile=auth.profile,
        has_env_auth=has_env_auth,
        has_home_env=has_home_env,
        auth_source_label=_auth_source_label(
            has_env_auth=has_env_auth,
            has_home_env=has_home_env,
            storage_path=storage_path,
        ),
        test_fetch=test_fetch,
        json_output=json_output,
        passive=passive,
    )


async def run_auth_check(plan: AuthCheckPlan) -> AuthCheckResult:
    """Execute an ``auth check`` plan and return the structured outcome.

    Thin adapter over :func:`notebooklm._app.auth_check.run_auth_check`: injects
    the consolidated ``read_env_auth_json`` accessor so the inline-auth read
    stays the single env-var SoT. The caller (command layer) renders the result
    and chooses an exit code.
    """
    return await _run_auth_check_core(plan, read_env_auth_json=read_env_auth_json)
