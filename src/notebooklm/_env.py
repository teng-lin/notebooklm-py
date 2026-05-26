"""Runtime environment helpers for NotebookLM endpoints and defaults.

Centralises lookup of environment variables that influence the live behavior
of the client. Keeping these here avoids scattering ``os.environ.get`` calls
across the codebase and gives each override a single, documented entry point.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://notebooklm.google.com"
PERSONAL_BASE_HOST = "notebooklm.google.com"
ENTERPRISE_BASE_HOST = "notebooklm.cloud.google.com"

_ALLOWED_BASE_HOSTS = frozenset({PERSONAL_BASE_HOST, ENTERPRISE_BASE_HOST})

STRICT_DECODE_ENV = "NOTEBOOKLM_STRICT_DECODE"


def is_strict_decode_enabled() -> bool:
    """Return True if strict-decode mode is enabled.

    Strict mode is the default since PR 13.9a: when
    ``NOTEBOOKLM_STRICT_DECODE`` is unset (or set to one of ``"1"``,
    ``"true"``, ``"True"``), schema-drift helpers — notably
    :func:`notebooklm.rpc.safe_index` — raise
    :class:`~notebooklm.exceptions.UnknownRPCMethodError` on descent failure
    so callers learn about Google-side shape drift immediately.

    Set ``NOTEBOOKLM_STRICT_DECODE=0`` (or any other non-truthy value
    such as ``"false"``, ``"False"``, ``"no"``, ``"off"``, or ``""``)
    to opt back into the legacy warn-and-return-``None`` fallback for
    one release window. Anything not in the truthy set ``{"1", "true",
    "True"}`` is treated as non-truthy. See
    ``docs/adr/0011-schema-validation-policy.md`` for the rationale and
    the opt-out retirement timeline.
    """
    return os.environ.get(STRICT_DECODE_ENV, "1") in ("1", "true", "True")


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
      (``RpcExecutor.build_url`` and ``_chat.ask``).
    * Language-aware ``ArtifactsAPI.generate_*`` calls when callers pass
      ``language=None`` to opt in to environment/default resolution. Omitting
      ``language`` in the public Python API keeps the historical ``"en"``
      artifact-language default.
    """
    raw = os.environ.get("NOTEBOOKLM_HL", "") or ""
    return raw.strip() or "en"
