"""Keep local fault scenarios independent of ambient NotebookLM settings."""

import asyncio
import sys
from collections.abc import Iterator

import pytest

from tests._fault_server.environment import isolated_environment

_SUBPROCESS_SCENARIOS = frozenset(
    {
        "adapter_cli_ambiguous_create",
        "adapter_cli_transient_read",
        "adapter_mcp_chat_start_disconnect",
        "adapter_mcp_download_disconnect",
        "adapter_rest_download_disconnect",
    }
)


@pytest.fixture
def event_loop_policy(request: pytest.FixtureRequest) -> asyncio.AbstractEventLoopPolicy:
    """Give only Windows subprocess owners a loop that supports child processes."""
    callspec = getattr(request.node, "callspec", None)
    scenario = None if callspec is None else callspec.params.get("scenario")
    if sys.platform == "win32" and scenario in _SUBPROCESS_SCENARIOS:
        # CLI imports select the Selector policy for HTTP compatibility, but
        # that loop cannot create subprocesses on Windows. These parent tests
        # only supervise children; each child's own policy remains unchanged.
        # pytest-asyncio installs/restores this policy around the single test.
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.fixture(autouse=True)
def isolated_fault_environment() -> Iterator[None]:
    # Pytest executes tests serially within each worker. One scope encloses
    # every concurrent cohort in a test; no task changes process environment.
    with isolated_environment():
        yield
