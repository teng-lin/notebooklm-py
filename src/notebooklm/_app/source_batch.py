"""Transport-neutral source **batch-add** policy: the URL cap + the fatal-vs-isolate
classifier that both the REST route and the MCP tool consult.

Batch add isolates *per-URL, user-input* failures (a bad URL / SSRF-blocked host /
not-found) as a per-entry error while the rest of the batch proceeds. A *service /
infrastructure* failure (expired auth, rate limiting, an upstream 5xx / transport
error) is NOT specific to one URL — folding it into a per-item result would report
success for what is really a top-level auth/rate-limit/server failure, so it must
abort the batch (the adapter re-raises and its top-level handler maps it).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` / ``fastmcp`` /
``server`` / ``fastapi`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
It therefore does NOT reach the server's ``CATEGORY_STATUS`` table (which imports
FastAPI); the fatal set is expressed over the neutral :class:`ErrorCategory` and
proven equal to that table's 401/429/>=500 partition by
``tests/server/test_source_batch_parity.py``.
"""

from __future__ import annotations

from .errors import ErrorCategory, classify

__all__ = ["MAX_BATCH_URLS", "batch_item_is_fatal"]

#: Max URL entries accepted by one batch add. Bounds how long a single request can
#: occupy one shared source-mutation slot (each entry is added sequentially).
MAX_BATCH_URLS = 20

#: Categories whose REST projection (server ``CATEGORY_STATUS``) is 401 / 429 / >=500
#: — a service/infra failure not specific to one URL. A per-item add hitting one must
#: ABORT the batch (re-raise) rather than isolate. Kept as an explicit neutral set so
#: ``_app`` never imports the FastAPI-tainted ``CATEGORY_STATUS``; the server-side
#: ``test_source_batch_parity`` proves this equals the status-derived partition, so a
#: future taxonomy change that would silently diverge fails there.
_FATAL_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTH,
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.CONFIG,
        ErrorCategory.DEPENDENCY,
        ErrorCategory.NETWORK,
        ErrorCategory.ARTIFACT_TIMEOUT,
        ErrorCategory.TIMEOUT,
        ErrorCategory.SERVER,
        ErrorCategory.RPC,
        ErrorCategory.LIBRARY,
        ErrorCategory.UNEXPECTED,
    }
)


def batch_item_is_fatal(exc: BaseException) -> bool:
    """Whether a per-item batch-add failure must abort the whole batch (re-raise).

    Fatal = the failure is an auth / rate-limit / server-side / infrastructure
    error (not specific to the one URL). ``CancelledError`` is a ``BaseException``
    and is never passed here (the adapters catch only ``Exception``).
    """
    return classify(exc).category in _FATAL_CATEGORIES
