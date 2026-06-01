"""Runtime RPC ID override policy for NotebookLM batchexecute calls."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

RPC_OVERRIDES_ENV_VAR = "NOTEBOOKLM_RPC_OVERRIDES"

# Track which override dicts we've already logged at INFO level, keyed by the
# hash of the canonical (sorted) item tuple. This dedupes multi-client tests
# while still emitting one INFO line per *distinct* override set in a process.
_logged_override_hashes: set[int] = set()


def _valid_rpc_method_names() -> set[str]:
    """Return valid RPCMethod member names without importing RPCMethod at module load."""
    from .types import RPCMethod

    return set(RPCMethod.__members__)


@lru_cache(maxsize=8)
def _parse_rpc_overrides(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse and validate the raw env-var string, returning an immutable mapping.

    Cached on the raw string so JSON parsing - and the WARNING emitted for
    malformed input - happens once per distinct env value rather than once
    per RPC call. Returns a tuple-of-pairs (immutable, hashable) so the
    caller can rebuild a fresh dict without mutating cached state.
    """
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("NOTEBOOKLM_RPC_OVERRIDES is not valid JSON: %s", exc)
        return ()
    if not isinstance(data, dict):
        logger.warning(
            "NOTEBOOKLM_RPC_OVERRIDES must be a JSON object mapping "
            "method names to RPC IDs, got %s",
            type(data).__name__,
        )
        return ()
    # Reject keys that don't match an RPCMethod enum member. Without this
    # gate, a typo like ``"LIST_NOTEBOOK"`` would silently no-op (resolver
    # only matches exact enum names) while the INFO log line proudly claims
    # the override was applied - making the escape hatch look live while
    # calls keep using canonical IDs. ``RPCMethod`` lives in ``rpc.types``;
    # the lookup is deferred so importing this module does not create a
    # runtime import cycle.
    valid_methods = _valid_rpc_method_names()
    normalized: list[tuple[str, str]] = []
    null_keys: list[str] = []
    for k, v in data.items():
        if v is None:
            # ``json.loads('{"X": null}')`` would coerce to ``str(None) ==
            # "None"`` - a literal four-character string on the wire, almost
            # certainly not what the user meant. Drop and warn loudly.
            null_keys.append(str(k))
            continue
        normalized.append((str(k), str(v)))
    if null_keys:
        logger.warning(
            "Ignoring NOTEBOOKLM_RPC_OVERRIDES entries with null values "
            "(provide a non-null RPC id string): %s",
            ", ".join(sorted(null_keys)),
        )
    unknown = sorted(k for k, _ in normalized if k not in valid_methods)
    if unknown:
        logger.warning(
            "Ignoring unknown NOTEBOOKLM_RPC_OVERRIDES method names (not in RPCMethod): %s",
            ", ".join(unknown),
        )
    return tuple((k, v) for k, v in normalized if k in valid_methods)


def _load_rpc_overrides() -> dict[str, str]:
    """Parse ``NOTEBOOKLM_RPC_OVERRIDES`` into a ``{method_name: rpc_id}`` map.

    The env var is a JSON object mapping :class:`RPCMethod` member names
    (e.g. ``"LIST_NOTEBOOKS"``) to override RPC ID strings. Any malformed
    input - invalid JSON, non-object top-level (array, string, etc.) -
    is logged at WARNING and treated as no overrides.

    Returns an empty dict when the env var is unset or invalid.
    """
    return dict(_parse_rpc_overrides(os.environ.get(RPC_OVERRIDES_ENV_VAR)))


def resolve_rpc_id(method_name: str, canonical_id: str) -> str:
    """Return the override RPC id for ``method_name`` when applicable, else ``canonical_id``.

    Overrides are sourced from the ``NOTEBOOKLM_RPC_OVERRIDES`` env var and
    are gated on the configured base host being on the allowlist
    (:data:`notebooklm._env._ALLOWED_BASE_HOSTS`). When the host is off the
    allowlist - which the strict ``get_base_url()`` validator already
    enforces, but we re-check here as defense in depth - overrides are
    ignored to avoid leaking custom RPC IDs to untrusted endpoints.

    The first time a distinct override set is consulted in a process, the
    full mapping is logged at INFO level so operators can confirm the
    config they intended is live. Subsequent calls with the same set are
    silent to avoid spamming multi-client tests / long-running daemons.

    Args:
        method_name: The :class:`RPCMethod` enum member name
            (e.g. ``"LIST_NOTEBOOKS"``).
        canonical_id: The fallback RPC ID - usually
            ``RPCMethod.<member>.value`` - returned when no override applies.

    Returns:
        Either the override RPC id (when an override is configured AND the
        host is on the allowlist) or ``canonical_id``.
    """
    # Local import to avoid a circular import at module-load time -
    # ``_env`` is dependency-free, but the public package ``notebooklm``
    # imports ``rpc.types`` during init, and ``_env`` ships from the same
    # package.
    from .._env import _ALLOWED_BASE_HOSTS, get_base_host

    try:
        host = get_base_host()
    except ValueError:
        # ``get_base_host()`` raises ``ValueError`` for a malformed
        # ``NOTEBOOKLM_BASE_URL`` (the only failure mode it documents).
        # Treat overrides as disabled in that case rather than crashing the
        # resolver - the URL builder itself will surface the real error to
        # the caller. A broader ``except Exception`` would mask unrelated
        # bugs in ``get_base_host`` during development.
        return canonical_id
    if host not in _ALLOWED_BASE_HOSTS:
        return canonical_id

    overrides = _load_rpc_overrides()
    if not overrides:
        return canonical_id

    key = hash(tuple(sorted(overrides.items())))
    if key not in _logged_override_hashes:
        _logged_override_hashes.add(key)
        logger.info(
            "NOTEBOOKLM_RPC_OVERRIDES applied: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(overrides.items())),
        )

    return overrides.get(method_name, canonical_id)
