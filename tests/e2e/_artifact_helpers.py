"""Shared artifact selectors for live E2E tests and their unit coverage."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from scripts._ci_progress import report

from notebooklm import Artifact
from notebooklm.client import NotebookLMClient

URL_BACKED_ARTIFACT_FAMILIES = frozenset({"audio", "video", "infographic", "slide_deck"})
URL_BACKED_STUDIO_TYPES = frozenset(
    family.replace("_", "-") for family in URL_BACKED_ARTIFACT_FAMILIES
)


def completed_download_candidates(
    artifacts: list[Artifact], family: str, *, backend: str
) -> list[Artifact]:
    """Return completed artifacts whose backend can attempt payload resolution."""

    if family not in URL_BACKED_ARTIFACT_FAMILIES:
        raise ValueError(f"artifact family is not URL-backed: {family}")
    hydrate_android_slide = backend == "android" and family == "slide_deck"
    candidates = [
        artifact
        for artifact in artifacts
        if not bool(getattr(artifact, "is_unclassified_type4", False))
        and artifact.kind == family
        and artifact.is_completed
        and (hydrate_android_slide or bool(artifact.url))
    ]
    return sorted(candidates, key=lambda artifact: bool(artifact.url), reverse=True)


def studio_item_may_have_download_payload(item: dict[str, object], *, backend: str) -> bool:
    """Check whether the selected backend can attempt Studio payload resolution."""

    item_type = item.get("type")
    hydrate_android_slide = backend == "android" and item_type == "slide-deck"
    return (
        item_type not in URL_BACKED_STUDIO_TYPES or hydrate_android_slide or bool(item.get("url"))
    )


def completed_interactive_mind_maps(artifacts: list[Artifact]) -> list[Artifact]:
    """Return only downloadable interactive mind-map artifacts."""
    return [
        artifact
        for artifact in artifacts
        if artifact.is_interactive_mind_map and artifact.is_completed
    ]


async def assert_copied_reference_artifacts(
    client: NotebookLMClient,
    notebook_id: str,
    *,
    required_families: set[str],
    require_interactive_mind_map: bool,
    timeout: float = 600,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Keep copied-content failures visible without preventing other E2E tests."""
    deadline = clock() + timeout
    while True:
        artifacts = await client.artifacts.list(notebook_id)
        completed = [artifact for artifact in artifacts if artifact.is_completed]
        families = {
            artifact.kind.value for artifact in completed if not artifact.is_unclassified_type4
        }
        missing = sorted(required_families - families)
        missing_payloads = sorted(
            family
            for family in required_families & families & URL_BACKED_ARTIFACT_FAMILIES
            if not completed_download_candidates(
                completed, family, backend=client.backends["artifacts"]
            )
        )
        if require_interactive_mind_map and not any(
            artifact.is_interactive_mind_map for artifact in completed
        ):
            missing.append("interactive_mind_map")
        issues = []
        if missing:
            issues.append("missing completed families: " + ", ".join(missing))
        if missing_payloads:
            issues.append("missing download payload families: " + ", ".join(missing_payloads))
        remaining = max(0.0, deadline - clock())
        detail = "; ".join(issues) if issues else "ready"
        report(
            f"Copied reference artifacts: {detail}; remaining={remaining:.0f}s",
            summary=not issues or remaining == 0,
        )
        if not issues:
            return
        assert remaining > 0, "Copied reference " + detail
        await sleep(min(30, remaining))
