"""Internal environment/default resolvers for NotebookLM runtime behavior.

Centralises lookup of environment variables that influence the live behavior
of the client. Keeping these here avoids scattering ``os.environ.get`` calls
across the codebase and gives each override a single, documented entry point.

This is an implementation module. Public configuration imports stay on
``notebooklm.config``, which deliberately re-exports only the supported subset
of endpoint/language helpers from here.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode, urlparse

DEFAULT_BASE_URL = "https://notebooklm.google.com"
PERSONAL_BASE_HOST = "notebooklm.google.com"
ENTERPRISE_BASE_HOST = "notebooklm.cloud.google.com"

_ALLOWED_BASE_HOSTS = frozenset({PERSONAL_BASE_HOST, ENTERPRISE_BASE_HOST})


def get_base_url() -> str:
    """Return the configured NotebookLM base URL.

    ``NOTEBOOKLM_BASE_URL`` is constrained to known Google-owned NotebookLM hosts
    because the value is used for authenticated requests.
    """
    configured = os.environ.get("NOTEBOOKLM_BASE_URL")
    raw = (configured.strip() if configured is not None else DEFAULT_BASE_URL).rstrip("/")
    if not raw:
        raw = DEFAULT_BASE_URL
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("NOTEBOOKLM_BASE_URL has an invalid port") from exc
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or host is None
        or host not in _ALLOWED_BASE_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        allowed = ", ".join(sorted(_ALLOWED_BASE_HOSTS))
        raise ValueError(f"NOTEBOOKLM_BASE_URL must use https and one of: {allowed}")
    return f"https://{host}"


def get_base_host() -> str:
    """Return the configured NotebookLM host."""
    return urlparse(get_base_url()).hostname or PERSONAL_BASE_HOST


DEFAULT_BL = "boq_labs-tailwind-frontend_20260301.03_p0"


def get_default_bl() -> str:
    """Return the NotebookLM ``bl`` (build label) URL parameter value.

    Reads the ``NOTEBOOKLM_BL`` environment variable; surrounding whitespace
    is stripped. Unset, empty, or whitespace-only values fall back to
    :data:`DEFAULT_BL`.

    The ``bl`` parameter is sent on the chat streaming endpoint
    (``ChatAPI.ask``) and pins the frontend build the request is associated
    with. Override via ``NOTEBOOKLM_BL`` when chasing a regression tied to
    a specific build snapshot.
    """
    raw = os.environ.get("NOTEBOOKLM_BL", "") or ""
    return raw.strip() or DEFAULT_BL


def get_default_language() -> str:
    """Return the user's preferred interface language.

    Reads the ``NOTEBOOKLM_HL`` environment variable. Surrounding whitespace
    is stripped; unset, empty, or whitespace-only values fall back to ``"en"``.

    This value is threaded into two places:

    * The ``hl`` URL query parameter on every batchexecute RPC call
      (``RpcExecutor.build_url`` and
      ``_chat.wire.build_streaming_chat_request``).
    * Language-aware ``ArtifactsAPI.generate_*`` calls when callers pass
      ``language=None`` to opt in to environment/default resolution. Omitting
      ``language`` in the public Python API keeps the historical ``"en"``
      artifact-language default.
    """
    raw = os.environ.get("NOTEBOOKLM_HL", "") or ""
    return raw.strip() or "en"


def get_notebooklm_region() -> str:
    """Return the NotebookLM region name configured in the environment."""
    region_env = os.environ.get("NOTEBOOKLM_REGION")
    region = region_env.strip() if region_env else ""
    return region or "global"


def build_cloud_scoped_url(
    path: str = "",
    *,
    authuser: int = 0,
    account_email: str | None = None,
    include_authuser: bool = False,
    extra_params: dict[str, str] | None = None,
) -> str:
    """Build a NotebookLM URL, automatically handling Cloud scoping and project/auth parameters."""
    base = get_base_url()
    params = {}
    if extra_params:
        params.update(extra_params)

    if include_authuser:
        from ._auth.account import format_authuser_value

        params["authuser"] = format_authuser_value(authuser, account_email)

    if base == "https://notebooklm.cloud.google.com":
        region = get_notebooklm_region()

        # Ensure path has a trailing slash if it is empty, to match legacy:
        # e.g., base_url/region/ instead of base_url/region
        url_path = f"{region}/" if not path else f"{region}/{path}"
        url = f"{base}/{url_path}"
        project_env = os.environ.get("NOTEBOOKLM_PROJECT")
        project = project_env.strip() if project_env else ""
        if project:
            params["project"] = project
    else:
        url = f"{base}/" if not path else f"{base}/{path}"

    if params:
        # urlencode sorted keys to remain deterministic
        return f"{url}?{urlencode(sorted(params.items()))}"
    return url
