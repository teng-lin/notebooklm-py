"""CLI error-contract tests (issue #1452, Phase 2).

These tests pin the **CLI boundary** of the error path: when the server returns
an error, a real CLI command must exit with the right code and (in ``--json``
mode) emit the ADR-0015 error envelope with the right machine ``code``. The
*client-level* exception mapping (HTTP status -> ``RateLimitError`` /
``ServerError`` / ``ClientError``) is already covered by
``tests/integration/test_error_paths_vcr.py``; this module is the layer above —
``cli/error_handler.py::handle_errors`` translating each exception into the
exit-code + envelope contract that automation depends on.

How the error responses are produced (reused infra, no live recording)
----------------------------------------------------------------------
The three synthetic-error cassettes already exist in ``tests/cassettes/`` and
are hand-written from the canonical shapes in
``tests/cassette_patterns.build_synthetic_error_response``:

* ``error_synthetic_429_rate_limit.yaml``  — one ``wXbhsf`` POST -> HTTP 429
* ``error_synthetic_500_server.yaml``       — one ``wXbhsf`` POST -> HTTP 500
* ``error_synthetic_stale_csrf.yaml``       — two ``wXbhsf`` POSTs -> HTTP 400

``wXbhsf`` is ``LIST_NOTEBOOKS`` (``rpc/types.py``), so every cassette replays
for ``notebooklm list`` — the CLI command exercised here. VCR matches on
``rpcids`` + body *shape* (not the notebook id), so the same cassettes that
``test_error_paths_vcr.py`` drives through the bare client replay identically
through the CLI -> Client -> RPC stack.

The retry seam (mirrors ``test_error_paths_vcr.py``)
----------------------------------------------------
A CLI-built ``NotebookLMClient`` ships with the default retry budget, so a
single-interaction error cassette would be re-POSTed and VCR would raise
``CannotOverwriteExistingCassetteException`` looking for a 2nd interaction. We
zero the retry budgets on the client the CLI constructs and no-op
``asyncio.sleep`` so the first synthetic error surfaces immediately. Because the
``list`` command resolves its client factory via ``resolve_client_factory(ctx,
default=NotebookLMClient)``, the seam is injected by seeding
``ctx.obj["client_factory"]`` with a thin zero-retry factory (passed through
``CliRunner.invoke(obj=...)``) — no module patching, no ADR-0007 monkeypatch
surface. The factory builds a REAL ``NotebookLMClient``, so it is supplied
directly rather than wrapped in the ``inject_client`` mock-instance helper.

Asserted mappings (verified against ``cli/error_handler.py``)
-------------------------------------------------------------
================  ====  =====================  =========
mode              HTTP  envelope ``code``      exit code
================  ====  =====================  =========
429               429   ``RATE_LIMITED``       1
5xx               500   ``NOTEBOOKLM_ERROR``   1
expired_csrf      400   ``NOTEBOOKLM_ERROR``   1
================  ====  =====================  =========

The 5xx and expired-csrf cases both land on the generic ``NOTEBOOKLM_ERROR``
branch: ``ServerError`` and ``ClientError`` derive from ``RPCError`` ->
``NotebookLMError`` and have no dedicated ``handle_errors`` branch, so the
catch-all ``except NotebookLMError`` (exit 1) handles them. ``429`` is the one
mode with a dedicated branch (``RATE_LIMITED``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.notebooklm_cli import cli

from .conftest import (
    ERROR_SCHEMA,
    assert_json_envelope,
    notebooklm_vcr,
    parse_json_output,
    skip_no_cassettes,
)

# All tests replay cassettes. ``skip_no_cassettes`` matches every other cli_vcr
# module: although the synthetic error cassettes are committed (so these
# normally run), the marker turns an absent-cassettes environment (partial
# checkout, future repo restructure) into a descriptive skip instead of a
# cryptic ``CassetteNotFoundError``.
pytestmark = [pytest.mark.vcr, skip_no_cassettes]


def _zero_retry_client(*args: Any, **kwargs: Any) -> NotebookLMClient:
    """Build a ``NotebookLMClient`` with every retry budget zeroed.

    Forwards ``*args``/``**kwargs`` to ``NotebookLMClient`` (the CLI calls it
    positionally with the resolved ``AuthTokens``) and only mutates the three
    retry-budget tunables on the live ``chain_host`` — the same seam
    ``test_error_paths_vcr.py`` rebinds. With the budgets at 0 the first
    synthetic error in a single-interaction cassette surfaces immediately
    instead of asking VCR for a non-existent 2nd interaction.
    """
    client = NotebookLMClient(*args, **kwargs)
    client._composed.chain_host._rate_limit_max_retries = 0
    client._composed.chain_host._server_error_max_retries = 0
    client._composed.chain_host._refresh_retry_delay = 0
    return client


def _install_zero_retry_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refresh_calls: list[object] | None = None,
) -> dict[str, Any]:
    """Build a zero-retry client factory + no-op ``asyncio.sleep``.

    Returns the ``ctx.obj`` payload (``{"client_factory": _factory}``) to pass to
    ``CliRunner.invoke(obj=...)`` — the injection seam (``resolve_client_factory``
    reads ``ctx.obj["client_factory"]`` first) replaces the old per-module
    ``NotebookLMClient`` rebind. Because ``_factory`` builds a REAL
    ``NotebookLMClient`` (not a mock), it is passed directly as the factory and
    NOT wrapped in ``inject_client`` (which is for client instances).
    ``asyncio.sleep`` is no-opped so any residual backoff (e.g. the refresh-retry
    delay) adds no wall-clock time.

    When ``refresh_calls`` is provided (the ``expired_csrf`` path), the factory
    also installs an in-process auth-refresh callback that issues NO HTTP and
    appends to that list on each invocation. Production refresh re-extracts
    CSRF/session from the homepage — an un-recorded GET leg — so the stub mutates
    the in-memory CSRF token instead, keeping the cassette to its two
    batchexecute POSTs (mirrors
    ``test_error_paths_vcr.test_expired_csrf_triggers_refresh``).
    """

    def _factory(*args: Any, **kwargs: Any) -> NotebookLMClient:
        client = _zero_retry_client(*args, **kwargs)
        if refresh_calls is not None:

            async def _stub_refresh() -> AuthTokens:
                refresh_calls.append(None)
                client._auth.csrf_token = "refreshed_csrf_token"
                client._collaborators.auth_coord.update_auth_headers(
                    auth=client._auth,
                    kernel=client._collaborators.kernel,
                )
                return client._auth

            client._collaborators.auth_coord._refresh_callback = _stub_refresh
        return client

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    return {"client_factory": _factory}


class TestRateLimited429:
    """``429`` -> ``RateLimitError`` -> envelope ``RATE_LIMITED``, exit 1."""

    def test_json_envelope(
        self,
        runner: Any,
        mock_auth_for_vcr: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``list --json`` on a 429 emits the ``RATE_LIMITED`` error envelope.

        ``handle_errors``' ``except RateLimitError`` branch maps the exception to
        ``code="RATE_LIMITED"`` with exit code 1, and surfaces the parsed
        ``Retry-After`` value (``1`` in the cassette) under the ``retry_after``
        extra field.
        """
        client_obj = _install_zero_retry_seam(monkeypatch)
        with notebooklm_vcr.use_cassette("error_synthetic_429_rate_limit.yaml") as cassette:
            result = runner.invoke(cli, ["list", "--json"], obj=client_obj)

        assert result.exit_code == 1, result.output
        assert_json_envelope(result, schema=ERROR_SCHEMA)
        data = parse_json_output(result.output)
        assert data is not None
        # ``.get()`` (not ``data[...]``) so a regression that drops a field
        # surfaces as a clean value mismatch instead of a ``KeyError`` traceback.
        assert data.get("error") is True
        assert data.get("code") == "RATE_LIMITED"
        # The Retry-After header (``1`` in the cassette) is threaded into the
        # envelope as a structural extra so automation can back off correctly.
        assert data.get("retry_after") == 1
        # Exactly one POST: the failing one. Equality (not ``>=``) catches a
        # regression where the zeroed retry budget silently re-enables itself.
        assert cassette.play_count == 1

    def test_text_exit_code(
        self,
        runner: Any,
        mock_auth_for_vcr: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``list`` (no ``--json``) on a 429 exits 1 with the human error line.

        The non-JSON branch writes ``"Error: Rate limited. Retry after 1s."`` to
        stderr (merged into ``result.output`` by ``CliRunner``) and exits 1 — no
        JSON envelope.
        """
        client_obj = _install_zero_retry_seam(monkeypatch)
        with notebooklm_vcr.use_cassette("error_synthetic_429_rate_limit.yaml"):
            result = runner.invoke(cli, ["list"], obj=client_obj)

        assert result.exit_code == 1
        assert "Rate limited" in result.output
        # Human mode must NOT emit a JSON envelope.
        assert parse_json_output(result.output) is None


class TestServerError5xx:
    """``5xx`` -> ``ServerError`` -> generic ``NOTEBOOKLM_ERROR``, exit 1.

    ``ServerError`` has no dedicated ``handle_errors`` branch; it derives from
    ``RPCError`` -> ``NotebookLMError`` and is caught by the catch-all
    ``except NotebookLMError`` -> ``code="NOTEBOOKLM_ERROR"``, exit 1.
    """

    def test_json_envelope(
        self,
        runner: Any,
        mock_auth_for_vcr: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``list --json`` on a 500 emits the generic ``NOTEBOOKLM_ERROR`` envelope."""
        client_obj = _install_zero_retry_seam(monkeypatch)
        with notebooklm_vcr.use_cassette("error_synthetic_500_server.yaml") as cassette:
            result = runner.invoke(cli, ["list", "--json"], obj=client_obj)

        assert result.exit_code == 1, result.output
        assert_json_envelope(result, schema=ERROR_SCHEMA)
        data = parse_json_output(result.output)
        assert data is not None
        assert data.get("error") is True
        assert data.get("code") == "NOTEBOOKLM_ERROR"
        assert cassette.play_count == 1

    def test_text_exit_code(
        self,
        runner: Any,
        mock_auth_for_vcr: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``list`` (no ``--json``) on a 500 exits 1 with the server-error line."""
        client_obj = _install_zero_retry_seam(monkeypatch)
        with notebooklm_vcr.use_cassette("error_synthetic_500_server.yaml"):
            result = runner.invoke(cli, ["list"], obj=client_obj)

        assert result.exit_code == 1
        # Tighter than a bare ``"Error:"``: pin the ``ServerError`` message
        # fragment so an unrelated error (e.g. an auth-setup failure) can't
        # satisfy the assertion.
        assert "Server error" in result.output
        assert parse_json_output(result.output) is None


class TestExpiredCsrf400:
    """``expired_csrf`` (HTTP 400) -> auth refresh -> ``NOTEBOOKLM_ERROR``, exit 1.

    HTTP 400 is NotebookLM's stale-CSRF response (``is_auth_error`` treats it as
    an auth-refresh trigger). The cassette holds TWO ``wXbhsf`` 400s: the first
    fires the auth-refresh middleware, the second (post-refresh) 400 ends the
    attempt as ``ClientError`` (a 4xx that is not 401/403/429/5xx). ``ClientError``
    is an ``RPCError`` -> ``NotebookLMError`` with no dedicated branch, so the
    CLI surfaces the generic ``NOTEBOOKLM_ERROR`` envelope, exit 1.
    """

    def test_json_envelope_after_refresh(
        self,
        runner: Any,
        mock_auth_for_vcr: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``list --json`` on a stale CSRF refreshes once, then emits ``NOTEBOOKLM_ERROR``."""
        refresh_calls: list[object] = []
        client_obj = _install_zero_retry_seam(monkeypatch, refresh_calls=refresh_calls)

        with notebooklm_vcr.use_cassette("error_synthetic_stale_csrf.yaml") as cassette:
            result = runner.invoke(cli, ["list", "--json"], obj=client_obj)

        assert result.exit_code == 1, result.output
        assert_json_envelope(result, schema=ERROR_SCHEMA)
        data = parse_json_output(result.output)
        assert data is not None
        assert data.get("error") is True
        assert data.get("code") == "NOTEBOOKLM_ERROR"
        # The auth-refresh branch fired exactly once for the one stale-CSRF call.
        assert len(refresh_calls) == 1
        # Two POSTs: the initial 400 and the post-refresh retry 400.
        assert cassette.play_count == 2

    def test_text_exit_code(
        self,
        runner: Any,
        mock_auth_for_vcr: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``list`` (no ``--json``) on a stale CSRF exits 1 after the refresh retry."""
        refresh_calls: list[object] = []
        client_obj = _install_zero_retry_seam(monkeypatch, refresh_calls=refresh_calls)

        with notebooklm_vcr.use_cassette("error_synthetic_stale_csrf.yaml") as cassette:
            result = runner.invoke(cli, ["list"], obj=client_obj)

        assert result.exit_code == 1
        # The post-refresh 400 surfaces as ``ClientError``; pin that fragment so
        # an unrelated error cannot satisfy the assertion.
        assert "Client error" in result.output
        assert parse_json_output(result.output) is None
        assert len(refresh_calls) == 1
        assert cassette.play_count == 2
