"""Cassette backfills for RPC methods that used to be coverage-gate gaps."""

import importlib.util
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType

import pytest

from notebooklm import NotebookLMClient, ResearchSource
from notebooklm.rpc import RPCMethod


def _load_test_helper(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"Could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load by path so mixed unit/integration selections cannot reuse tests/unit/conftest.py.
_integration_conftest = _load_test_helper(
    "notebooklm_rpc_gap_integration_conftest",
    Path(__file__).resolve().parent / "conftest.py",
)
_vcr_config = _load_test_helper(
    "notebooklm_rpc_gap_vcr_config",
    Path(__file__).resolve().parent.parent / "vcr_config.py",
)

get_vcr_auth = _integration_conftest.get_vcr_auth
skip_no_cassettes = _integration_conftest.skip_no_cassettes
notebooklm_vcr = _vcr_config.notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

MUTABLE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
)
RESEARCH_TASK_ID = "task_backfill_001"
RESEARCH_SOURCE_TITLE = "research backfill source"

# These raw ID assertions protect the static method-coverage gate's literal-id path.


@asynccontextmanager
async def vcr_client():
    auth = await get_vcr_auth()
    async with NotebookLMClient(auth) as client:
        yield client


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sources_refresh_direct.yaml")
async def test_refresh_source_rpc_has_cassette_coverage():
    async with vcr_client() as client:
        refreshed = await client.sources.refresh(MUTABLE_NOTEBOOK_ID, "source_backfill_001")

    assert refreshed is True
    assert RPCMethod.REFRESH_SOURCE.value == "FLmJqe"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_import_sources_direct.yaml")
async def test_import_research_rpc_has_cassette_coverage():
    source = ResearchSource(
        url="https://example.com/research-backfill",
        title=RESEARCH_SOURCE_TITLE,
        research_task_id=RESEARCH_TASK_ID,
    )

    async with vcr_client() as client:
        imported = await client.research.import_sources(
            MUTABLE_NOTEBOOK_ID,
            RESEARCH_TASK_ID,
            [source],
        )

    assert imported == [{"id": "imported_source_001", "title": RESEARCH_SOURCE_TITLE}]
    assert RPCMethod.IMPORT_RESEARCH.value == "LBwxtb"
