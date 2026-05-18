"""Synthetic HTTP error injection for VCR cassette recording (test-only).

Wires an opt-in :class:`_SyntheticErrorTransport` into the client's HTTP layer
when ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set to one of ``429`` / ``5xx`` /
``expired_csrf``. The substituted response shape matches what the client's
exception mapping keys on — see
``tests/cassette_patterns.py:build_synthetic_error_response``.

**Production behavior is unchanged when the env var is unset.** The transport
wrapper is only constructed when the env var resolves to a valid mode; the
default ``httpx.AsyncClient`` is built with no explicit transport otherwise.

This is deliberately wired through the client's HTTP layer (not just the VCR
config) so the substitution sits BELOW VCR — VCR records the synthetic response
into the cassette as if it had come from the wire. Wiring it at the VCR-config
layer only would mean the substitution never ran in record mode, leaving the
plumbing inert (the Momus iter-1 rejection rationale).

Public-on-private contract: :class:`_SyntheticErrorTransport`,
:func:`_get_error_injection_mode`, and :data:`ERROR_INJECT_ENV_VAR` are
re-exported from :mod:`notebooklm._core` so ``from notebooklm._core import …``
keeps working in ``tests/unit/test_vcr_config.py``, ``tests/conftest.py``, and
``tests/unit/test_core_lifecycle.py`` (which also monkeypatches
``_get_error_injection_mode`` through the ``_core`` module attribute).
:mod:`notebooklm._core_lifecycle` resolves both symbols via
``from . import _core as _core_module; _core_module._get_error_injection_mode()``
at call time so the monkeypatch surface remains hot.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx

# Logger name pinned to ``notebooklm._core`` (not the literal module name) so
# log filters in tests — e.g. ``caplog.at_level(..., logger="notebooklm._core")``
# — keep matching after this extraction. Mirrors the same pin in
# :mod:`notebooklm._core_lifecycle`.
logger = logging.getLogger("notebooklm._core")


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

    P1-12: ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is documented as test-only — it
    substitutes synthetic error responses for every batchexecute RPC. Before
    this guard, a leaked deploy env (e.g. an unset-on-prod CI variable that
    slipped through) would silently wrap the production transport in
    :class:`_SyntheticErrorTransport`, returning fake 429/5xx/expired_csrf
    responses to live callers.

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


class _SyntheticErrorTransport(httpx.AsyncBaseTransport):
    """Test-only httpx transport that substitutes synthetic error responses.

    Wraps an inner ``httpx.AsyncBaseTransport`` and substitutes a synthetic
    error response on outgoing batchexecute POSTs, built by
    ``tests.cassette_patterns.build_synthetic_error_response``. Non-batchexecute
    traffic (Scotty uploads, ``RotateCookies`` pokes, the homepage GET that
    extracts CSRF) passes through unchanged because none of those endpoints
    are in scope for error-shape cassettes.

    Substitution scope is controlled by ``always``:

    - ``always=True`` (the default for record-mode use): every batchexecute
      POST is substituted. This matters because the client's auth-refresh
      path re-issues the same RPC; we want the SAME error to fire on every
      retry inside the recording window so the cassette captures the full
      retry-and-fail sequence rather than substituting once and then letting
      a real response slip through on the retry.
    - ``always=False``: only the FIRST batchexecute POST is substituted; later
      POSTs fall through to the inner transport. Useful for tests that want
      to assert the client recovers after a single transient failure.

    This class is OPT-IN — ``ClientCore`` only wraps the transport when
    ``_get_error_injection_mode()`` returns a non-``None`` value, so removing
    the env var restores byte-for-byte production behavior.
    """

    def __init__(
        self,
        mode: str,
        inner: httpx.AsyncBaseTransport,
        *,
        always: bool = True,
    ):
        self._mode = mode
        self._inner = inner
        self._always = always
        self._fired = False
        # Resolved lazily on first use so this module doesn't import the test
        # tree at module load time.
        self._builder: Callable[[str], tuple[int, bytes, dict[str, str]]] | None = None

    def _is_batchexecute(self, request: httpx.Request) -> bool:
        # NotebookLM's batchexecute endpoint lives under
        # ``notebooklm.google.com/_/LabsTailwindUi/data/batchexecute``. We
        # match on the path suffix so any subdomain / region variant still
        # triggers substitution.
        return request.url.path.endswith("/batchexecute")

    def _load_builder(
        self,
    ) -> Callable[[str], tuple[int, bytes, dict[str, str]]]:
        if self._builder is not None:
            return self._builder
        # Import lazily and via importlib to avoid a hard dependency on the
        # tests tree from production code. The env var that gates this whole
        # path is itself test-only, so this import only ever runs in
        # recording / unit-test contexts.

        # Walk up from src/notebooklm/_core_error_injection.py to the repo
        # root, then dive into tests/cassette_patterns.py. This keeps the
        # lookup robust to installed-package layouts (where ``tests/`` may
        # not exist) — in that case we raise a clear error rather than
        # silently no-oping.
        repo_root = Path(__file__).resolve().parent.parent.parent
        target = repo_root / "tests" / "cassette_patterns.py"
        if not target.exists():
            raise RuntimeError(
                f"{ERROR_INJECT_ENV_VAR} is set but "
                f"tests/cassette_patterns.py is not available at {target}. "
                f"This plumbing is test-only — unset {ERROR_INJECT_ENV_VAR} "
                f"to restore normal behavior."
            )
        spec = importlib.util.spec_from_file_location("_notebooklm_cassette_patterns", target)
        # NOT ``assert`` — runtime invariant must survive ``python -O``. The
        # check is defensive (spec_from_file_location on an existing .py file
        # virtually always succeeds) but if it ever fails the user has clear
        # remediation via the env var.
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Failed to load module spec for {target}. "
                f"Unset {ERROR_INJECT_ENV_VAR} to restore normal behavior."
            )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._builder = cast(
            Callable[[str], tuple[int, bytes, dict[str, str]]],
            mod.build_synthetic_error_response,
        )
        return self._builder

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Substitute ONLY on POST batchexecute calls. Non-POST traffic on the
        # same path (a hypothetical GET batchexecute probe, OPTIONS preflight,
        # etc.) is out of scope for error-shape cassettes and must pass through
        # unchanged — see CodeRabbit feedback on PR #638.
        if (
            request.method.upper() == "POST"
            and self._is_batchexecute(request)
            and (self._always or not self._fired)
        ):
            self._fired = True
            status_code, body, headers = self._load_builder()(self._mode)
            response = httpx.Response(
                status_code=status_code,
                headers=headers,
                content=body,
                request=request,
            )
            # ``httpx.Response`` constructed this way is already "read" — VCR
            # can serialize it directly via its standard before_record hook.
            return response
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
