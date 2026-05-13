from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifacts import ArtifactsAPI, GenerationStatus
from notebooklm.rpc import AuthError, NetworkError, RPCTimeoutError


@pytest.fixture
def api():
    core = MagicMock()
    notes_api = MagicMock()
    return ArtifactsAPI(core, notes_api)


@pytest.mark.asyncio
async def test_wait_for_completion_retry_success(api):
    # Mock poll_status to fail twice then succeed
    status_ready = GenerationStatus(task_id="task1", status="completed")

    api.poll_status = AsyncMock()
    api.poll_status.side_effect = [
        NetworkError("transient net"),
        RPCTimeoutError("transient timeout"),
        status_ready,
    ]

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        # Also need to patch asyncio.get_running_loop().time() to avoid timeout
        # but here we just test the retry logic
        result = await api.wait_for_completion("nb1", "task1", timeout=60.0)

        assert result == status_ready
        assert api.poll_status.call_count == 3
        assert mock_sleep.call_count == 2
        # Backoff: 2^1=2, 2^2=4
        mock_sleep.assert_any_call(2.0)
        mock_sleep.assert_any_call(4.0)


@pytest.mark.asyncio
async def test_wait_for_completion_retry_exhausted(api):
    api.poll_status = AsyncMock()
    api.poll_status.side_effect = NetworkError("persistent fail")

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(NetworkError, match="persistent fail"):
            await api.wait_for_completion("nb1", "task1", timeout=60.0)

        # Initial call + 3 retries = 4 total calls
        assert api.poll_status.call_count == 4


@pytest.mark.asyncio
async def test_wait_for_completion_no_retry_on_auth_error(api):
    api.poll_status = AsyncMock()
    api.poll_status.side_effect = AuthError("auth fail")

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        with pytest.raises(AuthError, match="auth fail"):
            await api.wait_for_completion("nb1", "task1", timeout=60.0)

        assert api.poll_status.call_count == 1
        assert mock_sleep.call_count == 0
