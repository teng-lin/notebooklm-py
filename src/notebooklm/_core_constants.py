"""Module-level constants for the NotebookLM core client.

Holds the ``DEFAULT_*`` knobs that historically lived in :mod:`notebooklm._core`'s
preamble. Each constant is re-exported from :mod:`notebooklm._core` so existing
``from notebooklm._core import DEFAULT_TIMEOUT`` imports keep working.

These values are tuned for typical interactive workloads; see each docstring
below for guidance on when an operator would want to override them via the
:class:`~notebooklm.NotebookLMClient` constructor kwargs.
"""

from __future__ import annotations

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
