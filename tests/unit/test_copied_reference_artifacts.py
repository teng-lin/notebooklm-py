"""Copied inventory is required; representation availability is reported separately."""

import warnings
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm import Artifact
from notebooklm._types.common import UnknownTypeWarning
from notebooklm.exceptions import RPCError
from tests.e2e._artifact_helpers import assert_copied_reference_artifacts


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


@asynccontextmanager
async def operation_scope(_label):
    yield SimpleNamespace(epoch=7)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["web", "android"])
async def test_reference_assertion_waits_for_late_artifacts(backend, capsys):
    clock = Clock()
    audio = Artifact(
        id="private-artifact", title="private-title", _artifact_type=1, status=3, url="private-url"
    )
    legacy = Artifact(
        id="private-legacy", title="private-title", _artifact_type=4, status=3, url="private-url"
    )
    client = SimpleNamespace(
        artifacts=SimpleNamespace(list=AsyncMock(side_effect=[[], [audio, legacy]])),
        backends={"artifacts": backend},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnknownTypeWarning)
        await assert_copied_reference_artifacts(
            client,
            "private-notebook",
            required_families={"audio"},
            require_interactive_mind_map=False,
            clock=clock,
            sleep=clock.sleep,
        )
    assert clock.now == 30
    output = capsys.readouterr().out
    assert "missing completed families: audio" in output
    assert "Copied reference artifacts: ready" in output
    assert "download_payload=available" in output
    assert "private-" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize("exact_url", [None, "private-url"])
@pytest.mark.parametrize(
    ("family", "type_code"), [("audio", 1), ("video", 3), ("infographic", 7), ("slide_deck", 8)]
)
async def test_inventory_ready_diagnoses_payload_without_polling(
    backend, family, type_code, exact_url, monkeypatch, tmp_path, capsys
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    clock = Clock()
    inventory = Artifact(
        id="private-artifact", title="private-title", _artifact_type=type_code, status=3
    )
    exact = Artifact(
        id=inventory.id, title=inventory.title, _artifact_type=type_code, status=3, url=exact_url
    )
    exact_read = AsyncMock(return_value=exact)
    client = SimpleNamespace(
        artifacts=SimpleNamespace(
            list=AsyncMock(return_value=[inventory]),
            _transport=SimpleNamespace(operation_scope=operation_scope),
            _get_studio_artifact=exact_read,
        ),
        backends={"artifacts": backend},
    )
    await assert_copied_reference_artifacts(
        client,
        "private-notebook",
        required_families={family},
        require_interactive_mind_map=False,
        clock=clock,
        sleep=clock.sleep,
    )
    assert clock.now == 0
    client.artifacts.list.assert_awaited_once()
    if backend == "android":
        exact_read.assert_awaited_once_with("private-notebook", inventory.id, expected_epoch=7)
    else:
        exact_read.assert_not_awaited()
    output = capsys.readouterr().out
    available = backend == "android" and exact_url is not None
    assert f"download_payload={'available' if available else 'unavailable'}" in output
    assert f"family={family}; completed=1; ListArtifacts_urls=0" in summary.read_text()
    assert ("does not validate download coverage" in output) is not available
    assert "private-" not in output + summary.read_text()


@pytest.mark.asyncio
async def test_exact_read_failure_remains_a_failure():
    audio = Artifact(id="private-artifact", title="private-title", _artifact_type=1, status=3)
    client = SimpleNamespace(
        artifacts=SimpleNamespace(
            list=AsyncMock(return_value=[audio]),
            _transport=SimpleNamespace(operation_scope=operation_scope),
            _get_studio_artifact=AsyncMock(side_effect=RPCError("read failed", rpc_code=13)),
        ),
        backends={"artifacts": "android"},
    )
    with pytest.raises(RPCError):
        await assert_copied_reference_artifacts(
            client,
            "private-notebook",
            required_families={"audio"},
            require_interactive_mind_map=False,
        )


@pytest.mark.asyncio
async def test_reference_assertion_fails_with_missing_families(monkeypatch, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    clock = Clock()
    client = SimpleNamespace(artifacts=SimpleNamespace(list=AsyncMock(return_value=[])))
    with pytest.raises(AssertionError, match="audio, video, interactive_mind_map"):
        await assert_copied_reference_artifacts(
            client,
            "private-notebook",
            required_families={"audio", "video"},
            require_interactive_mind_map=True,
            timeout=60,
            clock=clock,
            sleep=clock.sleep,
        )
    assert clock.now == 60
    assert "missing completed families: audio, video, interactive_mind_map" in summary.read_text()
    assert "private-notebook" not in capsys.readouterr().out
