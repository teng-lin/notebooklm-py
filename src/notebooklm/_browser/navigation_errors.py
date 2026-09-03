"""Classification of browser navigation failures.

A cohesive pure classifier leaf, in the same spirit as
``browser_launch_errors.py``: no CLI, no I/O, no Playwright import. It answers
one question — what KIND of failure is this Playwright error message — for the
browser-capture core, which re-exports the names for private import continuity.

Split out of ``browser_capture.py`` (ADR-0008 module-size budget) when #2257
added the navigation-failure tolerance. Deliberately does NOT take the
login-wait tracing that ADR-0033 PR 4.1 absorbed into that module; only these
string predicates move.

The central fact these predicates encode: the ``net::ERR_*`` family is not
uniform, and which half of it is benign depends on WHO issued the navigation.
See :func:`is_navigation_race` (we navigated) vs :func:`is_navigation_failure`
(we are watching a human navigate).
"""

from __future__ import annotations

import re

# Playwright TargetClosedError substring — matches the default message from
# Playwright's TargetClosedError class (introduced in v1.41). If a future
# version changes this message, the error will propagate unhandled (safe fallback).
TARGET_CLOSED_ERROR = "Target page, context or browser has been closed"
_NAVIGATION_INTERRUPTED_MARKERS = (
    "navigation interrupted",
    "interrupted by another navigation",
)
# Chromium aborts a pending navigation with this prose and NO ``net::`` code when
# a beforeunload dialog is dismissed (``crPage.js`` -> ``frameAbortedNavigation``).
# Broad predicate only: where WE navigate, a cancelled goto did not happen, and
# proceeding on a stale ``page.url`` is the trap the headless arm guards against.
_ABORTED_NAVIGATION_MARKERS = ("navigation cancelled by beforeunload dialog",)
# Chromium reports a failed navigation as a ``net::ERR_*`` code in the Playwright
# message. Parsing the code out (rather than matching literals) is what lets the
# two predicates below disagree about the SAME error: which half of the family is
# benign depends on who issued the navigation. See :func:`is_navigation_race` vs
# :func:`is_navigation_failure`.
_NET_ERROR_PATTERN = re.compile(r"\bnet::ERR_[A-Z0-9_]{1,64}")


def is_navigation_interrupted_error(error: str | Exception) -> bool:
    """Return True for Playwright navigation races that are safe to ignore."""
    error_str = str(error).lower()
    return any(marker in error_str for marker in _NAVIGATION_INTERRUPTED_MARKERS)


def navigation_error_code(error: str | Exception) -> str | None:
    """Return the ``net::ERR_*`` code inside a Playwright error, else ``None``.

    Exists so failed navigations can be *logged* without logging the message
    they came in. Playwright embeds the offending URL in navigation errors
    (``net::ERR_ABORTED at https://…?f.sid=…``), and the code is the whole
    diagnostic — ``net::ERR_ABORTED`` (a benign redirect race) reads very
    differently from ``net::ERR_NAME_NOT_RESOLVED`` (the network dropped),
    while the URL only adds leak surface. Same reasoning as
    :func:`_log_suppressed`, which keeps the exception type and drops the rest.
    """
    match = _NET_ERROR_PATTERN.search(str(error))
    return match.group(0) if match else None


def is_navigation_race(error: str | Exception) -> bool:
    """Return True for a navigation *superseded* by another — safe to ignore.

    The narrow predicate, for the sites where **we** issued the navigation (the
    landing retry loop, cookie forcing). There a failure is our failure, so only
    the race class may be swallowed: ``ERR_ABORTED`` means something replaced
    the request — the benign outcome #214 and #322 widened this matcher for.

    Emphatically NOT the whole ``net::ERR_*`` family: ``ERR_INVALID_URL`` is a
    configuration fault that must fail fast, and ``ERR_CONNECTION_REFUSED`` must
    stay visible too — swallowing either turns a real error into a silent hang.
    Note the latter is NOT in :data:`RETRYABLE_CONNECTION_ERRORS` (only
    ``…CLOSED``/``…RESET`` are), so it reaches neither
    :func:`connection_error_help` nor this predicate. That routing gap predates
    this; do not "fix" it by widening here.
    """
    if TARGET_CLOSED_ERROR in str(error):
        return False
    return navigation_error_code(error) == "net::ERR_ABORTED" or is_navigation_interrupted_error(
        error
    )


def is_navigation_failure(error: str | Exception) -> bool:
    """Return True when a Playwright error is any failed navigation.

    The broad predicate, for the login wait alone. There we are not navigating —
    we are *watching a human* navigate — so a failed hop says nothing about their
    sign-in. A DNS blip is no grounds to tear down a five-minute wait, hence the
    whole ``net::ERR_*`` family where :func:`is_navigation_race` takes only races.

    Excludes ``TargetClosed`` on both predicates: a dead browser cannot be
    waited on, and every caller routes it to :data:`BROWSER_CLOSED_HELP`.
    """
    if TARGET_CLOSED_ERROR in str(error):
        return False
    if navigation_error_code(error) is not None or is_navigation_interrupted_error(error):
        return True
    lowered = str(error).lower()
    return any(marker in lowered for marker in _ABORTED_NAVIGATION_MARKERS)


__all__ = [
    "TARGET_CLOSED_ERROR",
    "is_navigation_failure",
    "is_navigation_interrupted_error",
    "is_navigation_race",
    "navigation_error_code",
]
