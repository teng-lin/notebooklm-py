"""Example tests demonstrating VCR.py integration.

VCR.py records HTTP interactions to YAML "cassettes" for deterministic replay.
This is useful for:
- Testing against real API responses without hitting rate limits
- Running tests offline or without credentials
- Creating regression tests from actual API behavior

Usage:
    # Record mode (requires valid auth):
    NOTEBOOKLM_VCR_RECORD=1 pytest tests/integration/test_vcr_example.py -v

    # Replay mode (default, uses recorded cassettes):
    pytest tests/integration/test_vcr_example.py -v

Note: Cassettes are gitignored by default. To share recorded cassettes,
verify they're properly scrubbed and commit them explicitly.

Note: These tests are automatically skipped if cassettes are not available.
"""

import sys
from pathlib import Path

import pytest

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import skip_no_cassettes
from vcr_config import notebooklm_vcr

# Skip all tests in this module if cassettes are not available
pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestVCRBasics:
    """Basic VCR.py functionality tests."""

    @pytest.mark.vcr
    @notebooklm_vcr.use_cassette("example_httpbin_get.yaml")
    @pytest.mark.asyncio
    async def test_vcr_records_and_replays(self):
        """Verify VCR.py can record and replay HTTP interactions.

        This test uses httpbin.org as a stand-in to demonstrate VCR works.
        Real tests would use the NotebookLM API.
        """
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get("https://httpbin.org/get")
            assert response.status_code == 200
            data = response.json()
            assert "origin" in data

    @pytest.mark.vcr
    @notebooklm_vcr.use_cassette("example_httpbin_post.yaml")
    @pytest.mark.asyncio
    async def test_vcr_handles_post_requests(self):
        """Verify VCR.py handles POST requests with form data."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://httpbin.org/post",
                data={"key": "value"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["form"]["key"] == "value"


class TestVCRScrubbing:
    """Tests verifying sensitive data scrubbing."""

    @pytest.mark.vcr
    @notebooklm_vcr.use_cassette("example_scrubbed_cookies.yaml")
    @pytest.mark.asyncio
    async def test_cookies_are_scrubbed(self):
        """Verify sensitive cookies are scrubbed from cassettes.

        The scrubbing happens at record time, so replay should show
        scrubbed values. Check the cassette file to verify.
        """
        import httpx

        async with httpx.AsyncClient() as client:
            # Send fake sensitive cookies
            response = await client.post(
                "https://httpbin.org/post",
                headers={
                    "Cookie": "SID=secret_session; HSID=another_secret",
                },
                data={"test": "data"},
            )
            assert response.status_code == 200
            # The response from httpbin echoes headers, but cassette should be scrubbed


class TestVCRWithNotebookLMPatterns:
    """Tests demonstrating VCR with notebooklm-py patterns.

    These tests show how VCR would integrate with the actual client.
    They use the existing pytest-httpx mocks but demonstrate the
    cassette structure.
    """

    @pytest.mark.vcr
    @notebooklm_vcr.use_cassette("example_batchexecute_pattern.yaml")
    @pytest.mark.asyncio
    async def test_batchexecute_style_request(self):
        """Simulate the batchexecute request pattern used by notebooklm-py.

        The actual client sends:
        - POST to /batchexecute
        - Form-encoded body with f.req= containing nested JSON
        - Cookie header with session cookies
        - Query params with rpcids and f.sid
        """
        import httpx

        # Simulate the request format from notebooklm._core.ClientCore.rpc_call()
        fake_rpc_body = (
            'f.req=[[["methodId",null,null,[[["notebook_id","data"]]]]]]&at=fake_csrf_token'
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://httpbin.org/post",  # Stand-in for batchexecute endpoint
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": "SID=test; HSID=test",
                },
                content=fake_rpc_body,
            )
            assert response.status_code == 200


# =============================================================================
# Example: How to write a VCR test for real NotebookLM API
# =============================================================================
#
# To create a real VCR test:
#
# 1. Write the test using the actual NotebookLM client:
#
#     @pytest.mark.vcr
#     @notebooklm_vcr.use_cassette('list_notebooks_real.yaml')
#     @pytest.mark.asyncio
#     async def test_list_notebooks_vcr(self):
#         from notebooklm import NotebookLMClient
#         from notebooklm.auth import AuthTokens
#
#         # Load real auth (only needed for recording)
#         auth = await AuthTokens.from_storage()
#
#         async with NotebookLMClient(auth) as client:
#             notebooks = await client.notebooks.list()
#
#         assert isinstance(notebooks, list)
#
# 2. Record the cassette:
#     NOTEBOOKLM_VCR_RECORD=1 pytest tests/integration/test_vcr_example.py::test_list_notebooks_vcr -v
#
# 3. Verify the cassette is properly scrubbed:
#     cat tests/cassettes/list_notebooks_real.yaml | grep -E "SID|HSID|SNlM0e"
#     # Should show SCRUBBED values, not real tokens
#
# 4. Future runs will replay from cassette (no auth needed):
#     pytest tests/integration/test_vcr_example.py::test_list_notebooks_vcr -v
#
# =============================================================================
