"""Module-level constants for the NotebookLM session client.

Holds the ``DEFAULT_*`` knobs that historically lived in the
``notebooklm._core`` preamble (the compatibility shim was removed in
v0.5.0). Callers import directly from this module — e.g.
``from notebooklm._session_config import DEFAULT_TIMEOUT``.

These values are tuned for typical interactive workloads; see each docstring
below for guidance on when an operator would want to override them via the
:class:`~notebooklm.NotebookLMClient` constructor kwargs.
"""

from __future__ import annotations

__all__ = [
    "CORE_LOGGER_NAME",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_KEEPALIVE_MIN_INTERVAL",
    "DEFAULT_MAX_CONCURRENT_RPCS",
    "DEFAULT_MAX_CONCURRENT_UPLOADS",
    "DEFAULT_TIMEOUT",
    "normalize_max_concurrent_uploads",
]

# Single source of truth for the logger name every session /
# middleware seam pins. Tests that filter logs via
# ``caplog.at_level(..., logger=CORE_LOGGER_NAME)`` (or, more commonly,
# the literal string) match this name. PR 12.9 audit fix: was previously
# repeated verbatim across seven modules; promoting it here eliminates
# the drift risk on rename. Callers do
# ``logger = logging.getLogger(CORE_LOGGER_NAME)``.
#
# The literal value is preserved as the historical ``"notebooklm._core"``
# logging key even though the ``_core`` compatibility shim was deleted in
# v0.5.0 — the logger is keyed by string, not module, and renaming would
# silently break every ``caplog.at_level("notebooklm._core", ...)`` site
# downstream. Treat this string as a public-ish logging contract.
CORE_LOGGER_NAME = "notebooklm._core"

# Default HTTP timeouts in seconds
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0  # Connection establishment timeout

# Minimum keepalive interval to avoid accidentally rate-limiting accounts.google.com
DEFAULT_KEEPALIVE_MIN_INTERVAL = 60.0

# Default ceiling on concurrent in-flight ``SourcesAPI.add_file`` uploads.
# Each in-flight upload holds one open file descriptor for the duration of
# the upload, so the cap is also an FD-exhaustion guard. Sized for typical
# interactive workloads; tune higher for batch ingestion pipelines that
# ingest dozens of files in parallel and have headroom in the process FD
# limit (``ulimit -n``).
DEFAULT_MAX_CONCURRENT_UPLOADS = 4

# Default ceiling on simultaneous in-flight ``_perform_authed_post``
# RPC POSTs. Sits *below* the default httpx pool
# size (``ConnectionLimits.max_connections=100``) so short-lived helper
# requests outside the RPC path — refresh GETs, resumable-upload
# preflights — have pool headroom even when the RPC semaphore is
# saturated. The default is intentionally conservative because
# batchexecute itself rate-limits aggressive fan-out; callers with a
# higher account tier (or an external rate-limiter) can opt out via
# ``max_concurrent_rpcs=None``.
DEFAULT_MAX_CONCURRENT_RPCS = 16


def normalize_max_concurrent_uploads(max_concurrent_uploads: int | None) -> int:
    """Normalize and validate the source-upload concurrency limit."""
    if max_concurrent_uploads is None:
        return DEFAULT_MAX_CONCURRENT_UPLOADS
    if max_concurrent_uploads < 1:
        raise ValueError(f"max_concurrent_uploads must be >= 1, got {max_concurrent_uploads!r}")
    return max_concurrent_uploads
