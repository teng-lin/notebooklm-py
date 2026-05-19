"""Synthetic HTTP error injection for VCR cassette playback (test-only).

When ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set to ``429`` / ``5xx`` /
``expired_csrf``, :class:`notebooklm._middleware_error_injection.ErrorInjectionMiddleware`
short-circuits each chain invocation with the synthetic response built
by ``tests/cassette_patterns.py:build_synthetic_error_response`` so the
client's exception-mapping branches (429 → ``RateLimitError``, 5xx →
``ServerError``, 400-CSRF → ``AuthError``) fire end-to-end.

**Production behavior is unchanged when the env var is unset.** The
middleware delegates straight to ``next_call``; the chain leaf
(``AuthedTransport.perform_authed_post``) runs exactly as it would
without the middleware in the chain.

Pre-Tier-12 history (deleted in PR 12.9): this module previously
defined a ``_SyntheticErrorTransport`` httpx transport that wrapped the
``AsyncClient`` BELOW VCR so the substituted response was recorded into
the cassette. Tier-12 PR 12.6 lifted the substitution into
``ErrorInjectionMiddleware`` (chain-level, ABOVE VCR), which closed the
"record synthetic errors into cassettes" workflow — replay-only is the
documented contract going forward; the synthetic-error cassettes in
``tests/cassettes/`` are hand-written from the canonical shapes in
``tests/cassette_patterns.py``. PR 12.9 deleted the legacy transport
class and its direct-instantiation tests.

Public surface kept:

- :func:`_get_error_injection_mode` — env-var → mode normalization.
- :func:`_refuse_synthetic_error_outside_test_context` —
  ``ClientCore.__init__`` calls this so a leaked deploy env raises
  ``RuntimeError`` instead of silently activating the chain
  middleware. The guard fires only when ``PYTEST_CURRENT_TEST`` is
  unset (pytest sets it for every test).
- :data:`ERROR_INJECT_ENV_VAR` — env-var name (canonical string).
"""

from __future__ import annotations

__all__ = [
    "ERROR_INJECT_ENV_VAR",
    "_get_error_injection_mode",
    "_refuse_synthetic_error_outside_test_context",
]

import logging
import os

from ._core_constants import CORE_LOGGER_NAME

# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level(..., logger=CORE_LOGGER_NAME)`` — keep
# matching. Every ``_core_*.py`` / ``_middleware_*.py`` seam shares the
# same name.
logger = logging.getLogger(CORE_LOGGER_NAME)


ERROR_INJECT_ENV_VAR = "NOTEBOOKLM_VCR_RECORD_ERRORS"


def _get_error_injection_mode() -> str | None:
    """Return the synthetic-error mode from ``NOTEBOOKLM_VCR_RECORD_ERRORS``.

    Returns ``None`` when the env var is unset, empty, or carries an
    unrecognized value (we deliberately fail open rather than crash a
    cassette-recording run on a typo — the unit tests catch the typo path,
    and the VCR config validates the value separately).

    The valid-mode set is hardcoded here (rather than imported from
    ``tests.cassette_patterns``) so production import time never reaches into
    the test tree. The same set is mirrored in
    ``tests.cassette_patterns.VALID_ERROR_MODES`` and the
    ``synthetic_error`` marker validator in ``tests/conftest.py``; the
    duplication is intentional and bounded — adding a fourth mode requires
    updating all three sites, which the unit tests in ``tests/unit/
    test_vcr_config.py`` will surface immediately.
    """
    raw = os.environ.get(ERROR_INJECT_ENV_VAR, "").strip()
    if not raw:
        return None
    # Lowercase-normalize so callers can use ``"5XX"`` / ``"429"`` / etc.
    normalized = raw.lower()
    valid = {"429", "5xx", "expired_csrf"}
    if normalized not in valid:
        return None
    return normalized


def _refuse_synthetic_error_outside_test_context() -> None:
    """Refuse :class:`ClientCore` instantiation when the test-only env var leaks.

    P1-12: ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is documented as test-only.
    Pre-Tier-12, leaving it set in a deploy env would silently wrap the
    production transport in the legacy ``_SyntheticErrorTransport``. After
    Tier-12 the activation moved into ``ErrorInjectionMiddleware``, but
    the operator-visible blast radius is the same (every chain call
    returns a fake response). The guard remains load-bearing.

    The guard fires only when:

    1. :func:`_get_error_injection_mode` returns a non-``None`` mode (so an
       empty / unrecognized env-var value still allows production startup),
       AND
    2. ``PYTEST_CURRENT_TEST`` is unset (pytest sets this for the lifetime
       of every test, including the ``@pytest.mark.synthetic_error`` fixture
       path that *does* legitimately set the env var).

    On refusal we log at WARNING with the env-var name and raise
    ``RuntimeError`` with the same env-var name so an operator can grep
    deploy configs and unset the offending variable.
    """
    mode = _get_error_injection_mode()
    if mode is None:
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # Legitimate pytest run — the ``@pytest.mark.synthetic_error``
        # fixture sets the env var inside a test context. Allow.
        return
    message = (
        f"{ERROR_INJECT_ENV_VAR}={mode!r} is set but no pytest context was "
        f"detected (PYTEST_CURRENT_TEST unset). This env var is test-only — "
        f"it substitutes synthetic error responses for every batchexecute "
        f"RPC and must not be set in production. Unset {ERROR_INJECT_ENV_VAR} "
        f"to restore normal behavior, or run under pytest if synthetic-error "
        f"recording is intended."
    )
    logger.warning(message)
    raise RuntimeError(message)
