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
import re
from datetime import date, datetime
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://notebook.google.com"
PERSONAL_BASE_HOST = "notebook.google.com"
ENTERPRISE_BASE_HOST = "notebooklm.cloud.google.com"

# The pre-rebrand personal host. Still served, still selectable via
# ``NOTEBOOKLM_BASE_URL``, and documented as the rollback lever for the #2067
# default flip -- Google dual-serves ``batchexecute`` on both personal hosts
# (ADR-0028), so pointing back at this one is a supported configuration rather
# than a workaround.
#
# It has its own literal-valued constant on purpose. Naming the default and the
# non-default host with one constant apiece is what keeps
# :data:`PERSONAL_APP_HOSTS` a two-element set; folding them together collapses
# it to one element and silently un-fixes #2015/#2020/#2038.
#
# Must stay a direct string literal: ``tests/_guardrails/
# test_app_host_literals_centralized.py`` reads it out of this module by AST.
PERSONAL_LEGACY_HOST = "notebooklm.google.com"

# Both hosts the personal app is served from. Built from the two literal-valued
# constants above -- never derive either from the other, and never derive them
# from this set: a ``frozenset`` cannot say which host plays which role, and the
# AST lint above requires both to be plain literals.
PERSONAL_APP_HOSTS = frozenset({PERSONAL_BASE_HOST, PERSONAL_LEGACY_HOST})

# Guard rather than derivation (#2067). If a future edit makes the two constants
# equal, this fails at import instead of silently shrinking every accept-set
# built from ``PERSONAL_APP_HOSTS``.
#
# Deliberately a raise, not an ``assert``: ``python -O`` strips assertions, so
# an assert would evaporate in exactly the optimized deployments where a silent
# one-element accept-set is hardest to notice. The guardrail test covers this at
# development time; this covers it at runtime.
if len(PERSONAL_APP_HOSTS) != 2:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "PERSONAL_BASE_HOST and PERSONAL_LEGACY_HOST must name different hosts; "
        "a one-element PERSONAL_APP_HOSTS silently breaks the login accept-set"
    )

_ALLOWED_BASE_HOSTS = PERSONAL_APP_HOSTS | {ENTERPRISE_BASE_HOST}


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


# The frontend build label sent as ``bl`` on the chat streaming endpoint.
#
# Re-captured live on 2026-08-04 from the app shell, which served
# ``…_20260802.02_p0`` from BOTH personal hosts (identical bytes — this is not a
# host-specific value). The previous pin, ``…_20260301.03_p0``, had gone five
# months without a re-check (#2073).
#
# Measured on the same day, the server does NOT validate this value: the live
# streaming endpoint returned a complete, cited answer for the pinned label, for
# the served label, AND for a fabricated ``…_19700101.00_p0``. So a stale pin is
# not degrading today — the risk is that it is accepted silently right up until
# it isn't, which is why :data:`BUILD_LABEL_STALE_AFTER_DAYS` exists and the
# nightly canary's build-label lane (``scripts/check_rpc_health.py``) watches the
# gap instead of leaving this constant unattended.
DEFAULT_BL = "boq_labs-tailwind-frontend_20260802.02_p0"

# How far behind the served label this pin may fall before the canary calls it
# stale. Wide on purpose: Google ships a new build roughly weekly, so a tighter
# window would alarm continuously and teach the operator to ignore the lane. The
# lane compares LABEL DATES, never the wall clock, so the verdict depends only on
# what was served.
BUILD_LABEL_STALE_AFTER_DAYS = 90

# Shape of the build label the app shell advertises: ``boq_labs-tailwind-frontend``
# followed by a ``YYYYMMDD.NN`` build stamp and a ``_pN`` patch suffix, each
# captured so the fields can be ordered numerically. Used to read the served label
# out of the shell HTML and to date it; a guardrail test asserts :data:`DEFAULT_BL`
# itself matches, so a malformed bump cannot land.
BUILD_LABEL_RE = re.compile(r"boq_labs-tailwind-frontend_(\d{8})\.(\d{2})_p(\d+)")


def extract_build_label(html: str) -> str | None:
    """Return the newest build label named in an app-shell response, if any.

    ``None`` means the text carried no recognizable label — a sign-in
    interstitial, an error page, or a shell whose label format changed. Callers
    must treat that as "no observation", never as "the label is empty".

    A healthy shell names exactly one label (measured 2026-08-04). Should it ever
    name several, the newest wins: the question this answers is "what is the
    freshest build Google is serving?", which is what :data:`DEFAULT_BL` is
    supposed to be tracking.

    Newest is decided on the parsed ``(date, build, patch)`` triple rather than by
    ordering the raw strings. The date and build fields are zero-padded fixed
    width, but the patch suffix is not — plain lexicographic order would rank
    ``_p9`` above ``_p10``.
    """
    matches = list(BUILD_LABEL_RE.finditer(html))
    if not matches:
        return None
    newest = max(matches, key=lambda m: (m.group(1), int(m.group(2)), int(m.group(3))))
    return newest.group(0)


def build_label_date(label: str) -> date | None:
    """Return the build date encoded in ``label``, or ``None`` if unreadable.

    Unreadable covers both a label that does not match :data:`BUILD_LABEL_RE`
    and one whose eight digits are not a real calendar date. Either way the
    caller has no basis for a staleness verdict and must not invent one.

    ``strptime`` rather than ``date.fromisoformat``: the compact ``YYYYMMDD``
    form only became valid ISO input in 3.11, and this package supports 3.10.
    """
    match = BUILD_LABEL_RE.search(label)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def build_label_days_behind(pinned: str, served: str) -> int | None:
    """Return how many days ``pinned`` trails ``served``, or ``None`` if undatable.

    Negative means the pin is *ahead* of what was served (a cohort served an
    older shell, say) — unusual, but not stale, and the caller must not read the
    magnitude as staleness.
    """
    pinned_date = build_label_date(pinned)
    served_date = build_label_date(served)
    if pinned_date is None or served_date is None:
        return None
    return (served_date - pinned_date).days


def get_default_bl() -> str:
    """Return the NotebookLM ``bl`` (build label) URL parameter value.

    Reads the ``NOTEBOOKLM_BL`` environment variable; surrounding whitespace
    is stripped. Unset, empty, or whitespace-only values fall back to
    :data:`DEFAULT_BL`.

    The ``bl`` parameter is sent on the chat streaming endpoint
    (``ChatAPI.ask``) and pins the frontend build the request is associated
    with. Override via ``NOTEBOOKLM_BL`` when chasing a regression tied to
    a specific build snapshot.

    An override does not change what the canary reports: the build-label lane
    always compares the *committed* :data:`DEFAULT_BL` against what the app shell
    serves, because that constant is what ships to every user.
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
