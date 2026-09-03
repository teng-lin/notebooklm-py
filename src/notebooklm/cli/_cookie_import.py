"""Cookie-JSON import helpers for ``notebooklm auth import-cookies``.

Split out of :mod:`notebooklm.cli.session_cmd` to keep that module under the
ADR-0008 module-size budget (and to keep stdlib names like ``shutil`` off its
retired patch surface). ``register_session_commands`` imports
:func:`_import_cookie_json` / :func:`_read_auth_json_input` back.

These are presentation-adjacent CLI helpers (they raise ``click.ClickException``
directly), so they live beside the command rather than in ``cli/services/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from .._app.login_cookie import (
    CookieImportFailure,
    CookieImportRequest,
    CookieImportSuccess,
    has_usable_secondary_binding,
    import_cookie_payload,
)
from ..auth import filter_storage_state_cookies_by_domain_policy, replace_profile_from_login

__all__ = ["_import_cookie_json", "_read_auth_json_input"]


def _read_auth_json_input(path: str) -> Any:
    """Read a cookie JSON payload from a file path or stdin (``-``)."""
    try:
        if path == "-":
            return json.loads(click.get_text_stream("stdin").read())
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise click.ClickException(  # cli-input-validation: import-cookies text decode failure
            f"Could not decode {path!r} as UTF-8: {exc}"
        ) from None
    except json.JSONDecodeError as exc:
        raise click.ClickException(  # cli-input-validation: import-cookies JSON parse failure
            f"Invalid JSON: {exc}"
        ) from None
    except OSError as exc:
        raise click.ClickException(  # cli-input-validation: import-cookies JSON read failure
            f"Could not read {path!r}: {exc}"
        ) from None


def _has_usable_secondary_binding(filtered_state: dict[str, Any]) -> bool:
    """Whether ``filtered_state`` carries a non-empty secondary auth binding.

    Restates the canonical rule from ``_auth.cookie_policy`` at the **value**
    level: that check counts a present-but-empty cookie as satisfying the
    binding, which import-cookies must not accept as a usable login.

    This is a deliberate copy, not an oversight. ``cli/`` may not import
    ``_private`` names out of public modules (``tests/_guardrails/
    test_cli_boundary.py``), and promoting an auth-policy predicate to the
    public surface is a bigger decision than this needs. The copy drifted once
    already — it kept the pre-``LSID`` rule when the canonical one gained its
    conjunct (#1977) — so ``test_cli_binding_rule_matches_cookie_policy`` pins
    the two together and fails if they diverge again.
    """
    try:
        return has_usable_secondary_binding(filtered_state)
    finally:
        del filtered_state


def _import_cookie_json(
    *,
    payload: Any,
    storage_path: Path,
    include_domains: set[str],
    include_optional: bool,
) -> tuple[dict[str, Any], Path | None]:
    """Validate, filter, and persist cookie JSON to ``storage_state.json``.

    Returns the persisted ``storage_state`` and the path of any ``.bak`` backup
    taken of a pre-existing target (``None`` when none was needed).
    """
    request = result = None
    try:
        request = CookieImportRequest(
            payload=payload,
            storage_path=storage_path,
            include_domains=include_domains,
            include_optional=include_optional,
        )
        result = import_cookie_payload(
            request,
            filter_storage_state=filter_storage_state_cookies_by_domain_policy,
            replace_profile_from_login=replace_profile_from_login,
        )
        if isinstance(result, CookieImportFailure):
            raise click.ClickException(result.message) from None  # cli-input-validation: policy
        assert isinstance(result, CookieImportSuccess)
        return result.storage_state, result.backup_path
    finally:
        del payload, storage_path, include_domains, include_optional, request, result
