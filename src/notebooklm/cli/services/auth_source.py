"""Single source of truth for resolving the active CLI auth source.

Historically, the question "where does authentication come from for this
command?" was answered by three independent reimplementations that all had
slightly different shapes:

* ``cli/context.py:_current_storage_override`` — reads ``ctx.obj["storage_path"]``
  (set by the root ``--storage`` Click option) and canonicalizes it.
* ``cli/helpers.py:_current_storage_override`` — a thin forwarding wrapper
  kept for backward compatibility with historical patch sites.
* ``cli/auth_runtime.py:_resolve_auth_storage_path`` — picks between the
  explicit ``storage_path`` and the ``NOTEBOOKLM_AUTH_JSON`` env-var
  fast-path before falling back to ``get_storage_path(profile=...)``.

The duplication was load-bearing: adding a new precedence rule (e.g.
"profile env var beats stored cookies") meant chasing the change through
all three call sites. `AuthSource` consolidates the precedence chain into
one immutable resolver so downstream readers ask one question:
"give me the resolved auth source for the current Click context."

Precedence (highest priority first):

1. **Explicit ``--storage <path>``** — wins everywhere. Treated as a
   complete override of the profile system: when set, the env-var fast
   path is skipped and the resolved path IS the storage file.
2. **``NOTEBOOKLM_AUTH_JSON`` env var** — inline JSON, no disk file.
   Returned as ``has_env_auth=True`` so callers can take the
   "no writable backing store" code path.
3. **Stored cookies under the active profile** — the default fallback.
   Resolved via ``paths.get_storage_path(profile=...)`` so each profile
   has its own ``storage_state.json``.

Single-import policy
====================

This module is the **only** site in ``src/notebooklm/cli/`` and
``src/notebooklm/notebooklm_cli.py`` that should read
``os.environ["NOTEBOOKLM_AUTH_JSON"]`` for precedence decisions. Other
callers route through :meth:`AuthSource.has_env_auth` or
:func:`auth_source_from_ctx`. Help strings and user-facing diagnostics
that mention the env var by name are fine — the policy targets *logic*
that branches on the env var, not text that names it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...paths import get_storage_path

if TYPE_CHECKING:
    import click


_NOTEBOOKLM_AUTH_JSON_ENV = "NOTEBOOKLM_AUTH_JSON"
# Public name for user-facing help text and error diagnostics. The CLI
# advertises this env var by name to script callers (CI/CD recipes) so
# the renderer needs to interpolate it. Centralising the literal keeps
# the consolidation gate clean.
AUTH_JSON_ENV_NAME = _NOTEBOOKLM_AUTH_JSON_ENV


def _read_env_auth_json() -> str | None:
    """Return the ``NOTEBOOKLM_AUTH_JSON`` env value, or ``None`` if unset."""
    return os.environ.get(_NOTEBOOKLM_AUTH_JSON_ENV)


@dataclass(frozen=True)
class AuthSource:
    """Resolved authentication source for a CLI invocation.

    Construct via :meth:`from_click_context` or :meth:`from_components`.
    Once built, the resolver is immutable; downstream code reads the
    pre-resolved values rather than re-running the precedence logic.

    Attributes:
        storage_override: The explicit ``--storage`` path (canonicalized
            via ``Path.expanduser().resolve()``) if the user passed one,
            else ``None``. When set, this is the complete auth override —
            ``has_env_auth`` is ignored for purposes of locating a file.
        profile: The active profile name from ``--profile``/``-p`` or the
            ``NOTEBOOKLM_PROFILE`` env var. ``None`` means "use the
            default profile resolution."
        has_env_auth: ``True`` when ``NOTEBOOKLM_AUTH_JSON`` is set to a
            non-empty value AND no ``--storage`` override is active.
            Callers that need a writable file should refuse / branch.
    """

    storage_override: Path | None
    profile: str | None
    has_env_auth: bool

    @classmethod
    def from_click_context(cls, ctx: click.Context | None) -> AuthSource:
        """Build an :class:`AuthSource` from a Click context.

        Reads ``ctx.obj["storage_path"]`` (set by the root ``--storage``
        option in :func:`notebooklm.notebooklm_cli.cli`) and
        ``ctx.obj["profile"]``. A ``None`` context or empty ``ctx.obj``
        is treated as "no overrides."
        """
        storage_override: Path | None = None
        profile: str | None = None
        if ctx is not None and ctx.obj:
            raw_storage = ctx.obj.get("storage_path")
            if raw_storage is not None:
                storage_override = Path(raw_storage).expanduser().resolve()
            profile = ctx.obj.get("profile")
        return cls.from_components(storage_override=storage_override, profile=profile)

    @classmethod
    def from_components(
        cls,
        *,
        storage_override: Path | None,
        profile: str | None,
    ) -> AuthSource:
        """Build an :class:`AuthSource` from pre-resolved components.

        Intended for callers (tests, CLI entry-point) that already hold
        the canonical ``--storage`` Path and profile name and don't want
        to round-trip through a Click context.
        """
        has_env_auth = storage_override is None and bool(_read_env_auth_json())
        return cls(
            storage_override=storage_override,
            profile=profile,
            has_env_auth=has_env_auth,
        )

    def resolve(self) -> Path | None:
        """Return the auth storage path to load, or ``None`` for env-only auth.

        Precedence:

        * ``storage_override`` if set → returned verbatim (it IS the auth
          file).
        * ``has_env_auth`` (``NOTEBOOKLM_AUTH_JSON`` set, no override) →
          returns ``None`` so the caller takes the env-var path
          (``build_httpx_cookies_from_storage(None)`` reads the env var
          directly).
        * Otherwise → returns the profile-resolved
          ``storage_state.json`` path from ``paths.get_storage_path``.
        """
        if self.storage_override is not None:
            return self.storage_override
        if self.has_env_auth:
            return None

        return get_storage_path(profile=self.profile)

    def storage_path_for_diagnostics(self) -> Path:
        """Return a concrete path for ``auth check`` / ``status`` diagnostics.

        Unlike :meth:`resolve`, this never returns ``None`` — when the
        env var supplies inline auth, the profile's nominal
        ``storage_state.json`` path is returned. Callers use the path to
        render "Storage file: <path>" in error messages and the auth-check
        table; the actual read is gated by :attr:`has_env_auth`.

        ``storage_override`` is already canonicalised by
        :meth:`from_click_context` and ``get_storage_path`` already returns
        an absolute resolved path, so no extra ``expanduser`` / ``resolve``
        is needed here.
        """
        if self.storage_override is not None:
            return self.storage_override
        return get_storage_path(profile=self.profile)


def auth_source_from_ctx(ctx: click.Context | None) -> AuthSource:
    """Module-level convenience for callers that don't want the classmethod.

    Equivalent to :meth:`AuthSource.from_click_context`.
    """
    return AuthSource.from_click_context(ctx)


def current_storage_override(ctx: click.Context | None) -> Path | None:
    """Return the active ``--storage`` override Path, or ``None``.

    Backward-compatibility surface for the legacy
    ``_current_storage_override()`` helpers in ``cli/helpers.py`` and
    ``cli/context.py``. The caller (a command-layer module) is responsible
    for resolving the live Click context via
    ``click.get_current_context(silent=True)`` and passing it in — this
    keeps the Click-context read out of ``cli/services`` per ADR-0008. A
    ``None`` context resolves to "no override."

    Public consumers should prefer :meth:`AuthSource.from_click_context`
    so they get the full resolver (env-var awareness, profile, etc.) in
    one shot. This shim exists to keep the historical patch surface
    stable; it does NOT consult ``NOTEBOOKLM_AUTH_JSON`` because the
    legacy contract was "return the explicit ``--storage`` only."
    """
    return AuthSource.from_click_context(ctx).storage_override


def has_env_auth_json() -> bool:
    """Return ``True`` when ``NOTEBOOKLM_AUTH_JSON`` is set to a non-empty value.

    The canonical question every CLI auth caller should ask instead of
    re-reading the env var inline. Storage override (``--storage``)
    suppression is NOT applied here — callers that need the combined
    precedence should construct an :class:`AuthSource` and read
    :attr:`AuthSource.has_env_auth`.
    """
    return bool(_read_env_auth_json())


def read_env_auth_json() -> str:
    """Return the ``NOTEBOOKLM_AUTH_JSON`` env value as a raw string.

    Callers MUST gate this read on :func:`has_env_auth_json`; calling
    when the env var is unset raises :class:`KeyError` (same contract as
    ``os.environ[...]``). Centralising the read here is the consolidation
    target — call sites that want the inline JSON payload route through
    this function instead of touching ``os.environ`` directly.
    """
    return os.environ[_NOTEBOOKLM_AUTH_JSON_ENV]


__all__ = [
    "AUTH_JSON_ENV_NAME",
    "AuthSource",
    "auth_source_from_ctx",
    "current_storage_override",
    "has_env_auth_json",
    "read_env_auth_json",
]
