"""Copied artifacts remain a hard assertion even after setup stops waiting on them."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm import Artifact
from tests.e2e._artifact_helpers import assert_copied_reference_artifacts


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


@pytest.mark.asyncio
async def test_reference_assertion_waits_for_late_artifacts(capsys):
    clock = Clock()
    audio = Artifact(
        id="private-artifact", title="private-title", _artifact_type=1, status=3, url="private-url"
    )
    client = SimpleNamespace(
        artifacts=SimpleNamespace(list=AsyncMock(side_effect=[[], [audio]])),
        backends={"artifacts": "web"},
    )
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
    assert "private-" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize(("family", "type_code"), [("audio", 1), ("slide_deck", 8)])
async def test_reference_waits_for_backend_download_payload(backend, family, type_code, capsys):
    clock = Clock()
    pending = Artifact(
        id="private-artifact", title="private-title", _artifact_type=type_code, status=3
    )
    ready = Artifact(
        id="private-artifact",
        title="private-title",
        _artifact_type=type_code,
        status=3,
        url="private-url",
    )
    client = SimpleNamespace(
        artifacts=SimpleNamespace(list=AsyncMock(side_effect=[[pending], [ready]])),
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
    hydrate_android_slide = backend == "android" and family == "slide_deck"
    assert clock.now == (0 if hydrate_android_slide else 30)
    output = capsys.readouterr().out
    assert ("missing download payload families" in output) is not hydrate_android_slide
    assert "private-" not in output


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
