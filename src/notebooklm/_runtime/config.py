"""Module-level constants for the NotebookLM client runtime.

Holds the ``DEFAULT_*`` knobs that historically lived in the
``notebooklm._core`` preamble (the compatibility shim was removed in
v0.5.0). Callers import directly from this module — e.g.
``from notebooklm._runtime.config import DEFAULT_TIMEOUT``.

These values are tuned for typical interactive workloads; see each docstring
below for guidance on when an operator would want to override them via the
:class:`~notebooklm.NotebookLMClient` constructor kwargs.
"""

from __future__ import annotations

import math
from typing import Any, cast

__all__ = [
    "AUTO_READ_TIMEOUT",
    "CORE_LOGGER_NAME",
    "DEFAULT_CHAT_RESPONSE_MAX_BYTES",
    "DEFAULT_CHAT_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT",
    "DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT",
    "DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT",
    "DEFAULT_KEEPALIVE_MIN_INTERVAL",
    "DEFAULT_MAX_CONCURRENT_RPCS",
    "DEFAULT_MAX_CONCURRENT_UPLOADS",
    "DEFAULT_TIMEOUT",
    "MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT",
    "assert_resolved_read_timeout",
    "compose_builtin_read_timeout",
    "normalize_max_concurrent_uploads",
    "resolve_chat_read_timeout",
    "resolve_import_research_read_timeout",
    "validate_read_timeout_kwarg",
]

# Single source of truth for the logger name every client-runtime /
# middleware seam pins. Tests that filter logs via
# ``caplog.at_level(..., logger=CORE_LOGGER_NAME)`` (or, more commonly,
# the literal string) match this name. Promoting it to a single constant
# here (it was previously repeated verbatim across modules) eliminates
# the drift risk on rename. Callers do
# ``logger = logging.getLogger(CORE_LOGGER_NAME)``.
#
# The literal value is preserved as the historical ``"notebooklm._core"``
# logging key even though the ``_core`` compatibility shim was deleted in
# v0.5.0 — the logger is keyed by string, not module, and renaming would
# silently break every ``caplog.at_level("notebooklm._core", ...)`` site
# downstream. Treat this string as a compatibility logging contract; it is
# not evidence that a concrete ``_core`` module or ``Session`` owner exists.
CORE_LOGGER_NAME = "notebooklm._core"

# Default HTTP timeouts in seconds
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0  # Connection establishment timeout
# Chat uses a streamed endpoint whose shared-notebook path can spend tens of
# seconds resolving access/source context before the first byte arrives. Keep
# fast metadata RPCs on the 30s read window, but give chat enough first-byte
# slack for the verified shared-notebook path.
DEFAULT_CHAT_TIMEOUT = 180.0

# Chat responses can include large notebook-state sync bytes in addition to
# the answer text. Keep ordinary metadata RPCs on the shared 50 MiB stream
# guard, but give chat a larger explicit default.
DEFAULT_CHAT_RESPONSE_MAX_BYTES = 256 * 1024 * 1024

# IMPORT_RESEARCH ingests every requested entry (fetch/parse/embed) server-side
# before responding to one RPC, so a large deep-research batch (the notebook
# source cap varies 50-600 by account tier) needs materially more time than
# the shared 30s metadata window. Scaled per requested source rather than
# flat-overridden so a small fast-research import still fails fast on a
# genuinely broken call; see :func:`resolve_import_research_read_timeout`
# (#2187).
DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT = 60.0
DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT = 3.0
DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT = 240.0

# Floor on the read window ``import_sources_with_verification`` will spend a
# retry on (#2205). Equal to the connect timeout on purpose: an attempt whose
# read window cannot outlast connection establishment can never observe its own
# result, and IMPORT_RESEARCH is ``NON_IDEMPOTENT_NO_RETRY`` — the server can
# still commit the sources the client never saw, duplicating them. So when less
# than this is left of ``max_elapsed``, the loop stops instead of dispatching a
# doomed attempt (which would also overrun its own deadline).
MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT = DEFAULT_CONNECT_TIMEOUT

# Minimum keepalive interval to avoid accidentally rate-limiting accounts.google.com
DEFAULT_KEEPALIVE_MIN_INTERVAL = 60.0

# Long-lived MCP and REST processes keep one client open for their full lifespan.
# Google's RotateCookies response advertises a 600-second next-rotation cadence,
# so server adapters opt into that interval instead of inheriting the general
# client default (``None``, which deliberately disables background work).
DEFAULT_SERVER_KEEPALIVE_INTERVAL = 600.0

# Default ceiling on concurrent in-flight ``SourcesAPI.add_file`` uploads.
# Each in-flight upload holds one open file descriptor for the duration of
# the upload, so the cap is also an FD-exhaustion guard. Sized for typical
# interactive workloads; tune higher for batch ingestion pipelines that
# ingest dozens of files in parallel and have headroom in the process FD
# limit (``ulimit -n``).
DEFAULT_MAX_CONCURRENT_UPLOADS = 4

# Default ceiling on simultaneous in-flight
# ``RuntimeTransport.perform_authed_post`` RPC POSTs. Sits *below* the default httpx pool
# size (``ConnectionLimits.max_connections=100``) so short-lived helper
# requests outside the RPC path — refresh GETs, resumable-upload
# preflights — have pool headroom even when the RPC semaphore is
# saturated. The default is intentionally conservative because
# batchexecute itself rate-limits aggressive fan-out; callers with a
# higher account tier (or an external rate-limiter) can opt out via
# ``max_concurrent_rpcs=None``.
DEFAULT_MAX_CONCURRENT_RPCS = 16


class _AutoReadTimeout:
    """Type of :data:`AUTO_READ_TIMEOUT`; exists only for its stable ``repr``.

    A bare ``object()`` renders as ``<object object at 0x...>``, and that
    address leaks into every signature snapshot that records parameter defaults
    (``scripts/audit_public_api_compat.py``,
    ``tests/_guardrails/test_auth_storage_compatibility.py``), making them
    unstable run to run.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "AUTO_READ_TIMEOUT"


# Sentinel meaning "the caller did not speak for this RPC's read window", used
# as the default of the per-RPC timeout kwargs (``chat_timeout``,
# ``import_research_timeout``). It exists because a plain
# ``chat_timeout: float | None = DEFAULT_CHAT_TIMEOUT`` cannot distinguish
# "left at the default" from "explicitly asked for 180 s" — and that
# distinction is exactly what the #2205 composition rule turns on. Typed
# ``Any`` so the public kwargs keep their honest ``float | None`` annotation:
# the sentinel is an implementation detail of "unset", not a value callers pass.
AUTO_READ_TIMEOUT: Any = _AutoReadTimeout()


def compose_builtin_read_timeout(builtin_window: float, base_timeout: float | None) -> float | None:
    """Compose a built-in per-RPC read window with the client's ``timeout=`` (#2205).

    The per-RPC constants (``DEFAULT_CHAT_TIMEOUT``, IMPORT_RESEARCH's
    batch-scaled window) exist to *lengthen* the shared 30 s metadata window
    for RPCs that legitimately need longer. They are defaults, never caps, so
    they may only ever raise the client-wide budget: a caller who buys
    ``timeout=600`` for every RPC is not silently cut back to 180 s / 240 s.

    Returning ``None`` means "impose no per-RPC read override, defer to the
    client's own configured base". That is the correct answer for
    ``base_timeout=None`` — a client built with no base read timeout at all
    (httpx reads ``None`` as "no timeout"), where a finite constant must not
    re-impose a ceiling the caller opted out of. It is deliberately *not* a
    claim that the request runs unbounded: what the transport does with "no
    override" is the transport's business (see ``Kernel.post``).
    """
    if base_timeout is None:
        return None
    return max(builtin_window, base_timeout)


def resolve_chat_read_timeout(
    chat_timeout: float | None, base_timeout: float | None
) -> float | None:
    """Resolve ``chat_timeout`` against the client's base ``timeout=`` (#2205).

    An explicit value — including ``None``, which means "inherit ``timeout=``
    verbatim" — is the caller's word and is honored as given, so a caller who
    deliberately shortens chat for fast failure still gets fast failure. Only
    the untouched default (:data:`AUTO_READ_TIMEOUT`) composes, and composing
    can only lengthen the window.
    """
    if chat_timeout is AUTO_READ_TIMEOUT:
        return compose_builtin_read_timeout(DEFAULT_CHAT_TIMEOUT, base_timeout)
    return chat_timeout


def resolve_import_research_read_timeout(
    source_count: int,
    *,
    base_timeout: float | None = DEFAULT_TIMEOUT,
    override: float | None = AUTO_READ_TIMEOUT,
    remaining_budget: float | None = None,
) -> float | None:
    """Resolve one IMPORT_RESEARCH attempt's timeout budget.

    The untouched default scales with the number of importable entries, is
    capped at :data:`DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT`, and is floored at
    the client's base timeout. An explicit ``override`` replaces both scaling
    and that floor; ``None`` inherits the transport's base timeout. A retry's
    remaining aggregate budget clamps the resolved window last.

    This transport-neutral resolver is shared by the web and Android
    implementations so both backends honor ``import_research_timeout`` with
    the same batch-scaling and retry-budget semantics.
    """
    window: float | None
    if override is AUTO_READ_TIMEOUT:
        window = compose_builtin_read_timeout(
            min(
                DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
                DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT
                + DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT * source_count,
            ),
            base_timeout,
        )
    else:
        window = override
    if remaining_budget is None:
        return window
    return remaining_budget if window is None else min(window, remaining_budget)


def validate_read_timeout_kwarg(value: Any, *, name: str) -> float | None:
    """Reject a per-RPC read-window kwarg that could only ever break the call.

    A misconfigured window is otherwise invisible: ``httpx.Timeout`` accepts
    ``read=0`` and ``read=-5`` without complaint, so the client would simply
    time out instantly on every affected RPC and report a generic transport
    timeout that names neither the kwarg nor the value (#2205). Mirrors the
    ``chat_response_max_bytes`` / ``max_concurrent_uploads`` checks on the same
    constructor.

    ``None`` ("inherit ``timeout=``") and :data:`AUTO_READ_TIMEOUT` ("unset")
    pass through untouched; anything else must be a positive real number.
    """
    if value is None or value is AUTO_READ_TIMEOUT:
        return cast("float | None", value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{name} must be a number of seconds, or None to inherit timeout= (got {value!r})"
        )
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name} must be a positive, finite number of seconds when supplied (got {value!r})"
        )
    return float(value)


def assert_resolved_read_timeout(value: Any, *, name: str) -> None:
    """Fail loudly if an unresolved :data:`AUTO_READ_TIMEOUT` reached a consumer.

    The sentinel is resolved once, at the composition root. Every layer below
    guards on ``is not None``, so a sentinel that slipped past would be handed
    to ``httpx.Timeout`` — which accepts it without error — and only surface far
    away, as a ``TypeError`` inside the timeout *error formatter*. This turns
    that into an immediate, named failure at the boundary it crossed.
    """
    if value is AUTO_READ_TIMEOUT:
        raise TypeError(
            f"{name} received the unresolved AUTO_READ_TIMEOUT sentinel; it must "
            "be resolved (resolve_chat_read_timeout) before reaching this layer"
        )


def normalize_max_concurrent_uploads(max_concurrent_uploads: int | None) -> int:
    """Normalize and validate the source-upload concurrency limit."""
    if max_concurrent_uploads is None:
        return DEFAULT_MAX_CONCURRENT_UPLOADS
    if max_concurrent_uploads < 1:
        raise ValueError(f"max_concurrent_uploads must be >= 1, got {max_concurrent_uploads!r}")
    return max_concurrent_uploads
