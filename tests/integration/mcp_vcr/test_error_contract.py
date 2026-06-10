"""MCP error-projection VCR tests (reuse-only).

Drives a tool over REAL synthetic-error cassettes and asserts the MCP tool
surfaces the structured ``CODE: <message> (retriable=<bool>)`` error — the
projection of a REAL decoded RPC error (not a mock exception) onto the MCP wire
contract (``mcp/_errors.py``).

The two synthetic-error cassettes are each one ``wXbhsf`` (``LIST_NOTEBOOKS``)
POST -> an HTTP error, the same recordings ``cli_vcr/test_error_contract.py``
and ``test_error_paths_vcr.py`` drive through the client:

* ``error_synthetic_500_server.yaml`` -> HTTP 500 -> ``ServerError`` ->
  ``ErrorCategory.SERVER`` -> MCP code ``SERVER`` (retriable).
* ``error_synthetic_429_rate_limit.yaml`` -> HTTP 429 -> ``RateLimitError`` ->
  ``ErrorCategory.RATE_LIMITED`` -> MCP code ``RATE_LIMITED`` (retriable).

``notebook_list`` is the MCP tool that issues ``LIST_NOTEBOOKS``. The client
built for these tests has its retry budgets zeroed (see
``conftest.build_zero_retry_mcp_client``) so the first recorded error surfaces
immediately instead of asking VCR for a non-existent second interaction.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_zero_retry_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


@pytest.mark.parametrize(
    ("cassette", "expected_code"),
    [
        ("error_synthetic_500_server.yaml", "SERVER"),
        ("error_synthetic_429_rate_limit.yaml", "RATE_LIMITED"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_rpc_error_projects_structured_code_over_vcr(
    cassette: str, expected_code: str
) -> None:
    """A recorded RPC error surfaces as the structured MCP tool error.

    End-to-end: FastMCP ``Client`` -> ``notebook_list`` tool ->
    ``client.notebooks.list()`` -> recorded ``LIST_NOTEBOOKS`` (``wXbhsf``) ->
    HTTP error -> the typed exception -> ``mcp_errors`` projects it to the
    structured ``ToolError`` message ``"<CODE>: <message> (retriable=true) ..."``.
    The code is the REAL projection of a REAL decoded RPC error, not a mock.
    """
    with pytest.raises(ToolError) as excinfo, notebooklm_vcr.use_cassette(cassette):
        async with build_zero_retry_mcp_client() as mcp_client:
            await mcp_client.call_tool("notebook_list", {})

    message = str(excinfo.value)
    # The structured contract: a leading machine code + the retriable flag,
    # projected from the REAL decoded RPC error (not a mock). Both modeled errors
    # are back-off (retriable) categories.
    assert message.startswith(f"{expected_code}:"), message
    assert "retriable=true" in message, message
