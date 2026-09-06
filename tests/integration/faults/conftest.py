"""Keep local fault scenarios independent of ambient NotebookLM settings."""

from collections.abc import Iterator

import pytest

from tests._fault_server.environment import isolated_environment


@pytest.fixture(autouse=True)
def isolated_fault_environment() -> Iterator[None]:
    # Pytest executes tests serially within each worker. One scope encloses
    # every concurrent cohort in a test; no task changes process environment.
    with isolated_environment():
        yield
