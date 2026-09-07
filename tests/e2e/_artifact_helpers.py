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


async def report_copied_download_payloads(
    client: NotebookLMClient,
    notebook_id: str,
    completed: list[Artifact],
    required_families: set[str],
) -> None:
    """Diagnose representations separately from the copied inventory contract.

    Android list summaries can omit payloads present in exact GetArtifact reads.
    Completed copied rows can also lack representations in both responses; the
    template contract promises completed families, not downloadable copies.
    """
    backend = client.backends["artifacts"]
    for family in sorted(required_families & URL_BACKED_ARTIFACT_FAMILIES):
        candidates = [
            artifact
            for artifact in completed
            if not artifact.is_unclassified_type4 and artifact.kind == family
        ]
        list_urls = sum(bool(artifact.url) for artifact in candidates)
        available = bool(list_urls)
        exact_reads = 0
        if not available and backend == "android":
            for candidate in candidates:
                async with client.artifacts._transport.operation_scope(
                    "e2e copied artifact representation"
                ) as lease:
                    exact = await client.artifacts._get_studio_artifact(
                        notebook_id, candidate.id, expected_epoch=lease.epoch
                    )
                exact_reads += 1
                if exact is not None and exact.kind == family and exact.is_completed and exact.url:
                    available = True
                    break
        report(
            f"Copied reference payload: family={family}; completed={len(candidates)}; "
            f"ListArtifacts_urls={list_urls}; GetArtifact_reads={exact_reads}; "
            f"download_payload={'available' if available else 'unavailable'}",
            summary=True,
        )
        if not available:
            report(
                f"WARNING: copied {family} inventory is complete but has no download URL; "
                "copy readiness does not validate download coverage",
                summary=True,
            )


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
    """Assert the copied inventory contract and report download payload availability."""
    deadline = clock() + timeout
    while True:
        artifacts = await client.artifacts.list(notebook_id)
        completed = [artifact for artifact in artifacts if artifact.is_completed]
        families = {
            artifact.kind.value for artifact in completed if not artifact.is_unclassified_type4
        }
        missing = sorted(required_families - families)
        if require_interactive_mind_map and not any(
            artifact.is_interactive_mind_map for artifact in completed
        ):
            missing.append("interactive_mind_map")
        issues = []
        if missing:
            issues.append("missing completed families: " + ", ".join(missing))
        remaining = max(0.0, deadline - clock())
        detail = "; ".join(issues) if issues else "ready"
        report(
            f"Copied reference artifacts: {detail}; remaining={remaining:.0f}s",
            summary=not issues or remaining == 0,
        )
        if not issues:
            await report_copied_download_payloads(client, notebook_id, completed, required_families)
            return
        assert remaining > 0, "Copied reference " + detail
        await sleep(min(30, remaining))
