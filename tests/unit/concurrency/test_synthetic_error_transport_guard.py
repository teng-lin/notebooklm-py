"""Tests for the synthetic-error transport env-var gate (P1-12).

Before P1-12, ``NOTEBOOKLM_VCR_RECORD_ERRORS`` was a fully open env var:
setting it outside a pytest run would silently wrap the production HTTP
transport in :class:`_SyntheticErrorTransport`, substituting synthetic
error responses for real RPC calls. The transport's docstring warned
about this but nothing actually gated against accidental production
exposure (e.g. a leaked environment variable in a deployment).

P1-12 closes that hole: ``NotebookLMClient`` construction (the constructor
consultation site) refuses instantiation when the env var is set without
``PYTEST_CURRENT_TEST`` in the environment, logging a WARNING and raising
``RuntimeError`` with remediation guidance. Tests legitimately set the
env var via the ``@pytest.mark.synthetic_error("…")`` fixture; pytest
always sets ``PYTEST_CURRENT_TEST`` during a test run, so the gate is
transparent in CI / unit test contexts.

Acceptance:
- Env var set + no ``PYTEST_CURRENT_TEST`` → ``RuntimeError`` from
  client instantiation.
- Env var set + ``PYTEST_CURRENT_TEST`` set → instantiation succeeds
  (the pytest path remains unchanged).
- Env var unset → instantiation succeeds (baseline production path).
- The refusal is logged at WARNING with the env-var name and remediation
  hint so an unexpected refusal surfaces in operator logs.
"""

from __future__ import annotations

import logging

import pytest

from notebooklm.auth import AuthTokens
from tests._helpers.client_factory import build_client_shell_for_tests


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


def test_synthetic_error_env_var_without_pytest_context_refuses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setting the env var without pytest context must raise.

    The test simulates a production process accidentally inheriting
    ``NOTEBOOKLM_VCR_RECORD_ERRORS=1`` (e.g. a leaked deploy env) by
    setting the env var and *unsetting* ``PYTEST_CURRENT_TEST`` for the
    constructor call. The refusal:

    1. Raises ``RuntimeError`` so the broken configuration surfaces at
       import / instantiation time rather than silently subbing in
       synthetic responses later.
    2. Mentions the env-var name in the error so the operator can
       grep deploy configs.
    3. Logs at WARNING so the refusal is visible in operator logs.
    """
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD_ERRORS", "5xx")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm._core"),
        pytest.raises(RuntimeError, match="NOTEBOOKLM_VCR_RECORD_ERRORS"),
    ):
        build_client_shell_for_tests(_make_auth())

    # WARNING must mention the env var so an operator can find the source.
    assert any(
        "NOTEBOOKLM_VCR_RECORD_ERRORS" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    ), (
        "expected WARNING log mentioning the env var; "
        f"got records={[(r.levelname, r.message) for r in caplog.records]}"
    )


def test_synthetic_error_env_var_with_pytest_context_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest context (PYTEST_CURRENT_TEST set) keeps instantiation working.

    The synthetic-error transport is a test-only opt-in path; pytest always
    sets ``PYTEST_CURRENT_TEST``, so the legitimate
    ``@pytest.mark.synthetic_error("…")`` fixture path must remain a
    no-op for the new gate.
    """
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD_ERRORS", "5xx")
    # PYTEST_CURRENT_TEST is set by pytest itself; assert it for clarity.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "fake_test")

    # Must NOT raise.
    core = build_client_shell_for_tests(_make_auth())
    assert core is not None


def test_synthetic_error_env_var_unset_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline production path — env var unset, instantiation works.

    No env var → :func:`_get_error_injection_mode` returns ``None`` and
    the new gate is bypassed. The synthetic transport is never wrapped.
    """
    monkeypatch.delenv("NOTEBOOKLM_VCR_RECORD_ERRORS", raising=False)
    # Even when pytest IS running (PYTEST_CURRENT_TEST set), no env var
    # means no gate check at all.
    core = build_client_shell_for_tests(_make_auth())
    assert core is not None


def test_synthetic_error_env_var_empty_string_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var set to empty string must NOT trigger the gate.

    ``_get_error_injection_mode`` returns ``None`` for empty/whitespace,
    so the synthetic transport never engages. The new gate must use the
    same predicate so accidental ``export NOTEBOOKLM_VCR_RECORD_ERRORS=``
    (empty value) does not block production startup.
    """
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD_ERRORS", "")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    core = build_client_shell_for_tests(_make_auth())
    assert core is not None
