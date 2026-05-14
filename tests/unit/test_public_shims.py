"""Tests for the public shim modules introduced by the private-module boundary plan.

Each section is owned by a different PR; please keep the markers below intact when
appending so concurrent PRs can merge cleanly.

Plan: .sisyphus/plans/private-module-boundary.md
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# PR-A section: notebooklm.research public surface
# ---------------------------------------------------------------------------


def test_research_module_exposes_documented_helpers():
    """notebooklm.research re-exports the three free helpers used by the CLI."""
    from notebooklm.research import (
        extract_report_urls,
        normalize_url,
        select_cited_sources,
    )

    assert callable(extract_report_urls)
    assert callable(normalize_url)
    assert callable(select_cited_sources)


def test_cited_source_selection_is_on_public_surface():
    """CitedSourceSelection lives in notebooklm.types and on the top-level package."""
    from notebooklm import CitedSourceSelection as TopLevel
    from notebooklm.types import CitedSourceSelection

    assert TopLevel is CitedSourceSelection


def test_research_select_cited_sources_returns_public_dataclass():
    """select_cited_sources returns the public CitedSourceSelection dataclass."""
    from notebooklm.research import select_cited_sources
    from notebooklm.types import CitedSourceSelection

    result = select_cited_sources([], "")
    assert isinstance(result, CitedSourceSelection)
    assert result.used_fallback is True


def test_research_api_backward_compat_classmethod_delegates():
    """notebooklm._research.ResearchAPI.select_cited_sources still works."""
    from notebooklm._research import ResearchAPI
    from notebooklm.types import CitedSourceSelection

    result = ResearchAPI.select_cited_sources([], "")
    assert isinstance(result, CitedSourceSelection)


def test_research_api_reexports_cited_source_selection_for_back_compat():
    """notebooklm._research.CitedSourceSelection continues to resolve."""
    from notebooklm._research import CitedSourceSelection as Legacy
    from notebooklm.types import CitedSourceSelection

    assert Legacy is CitedSourceSelection


# ---------------------------------------------------------------------------
# PR-D section: notebooklm.config / notebooklm.urls / notebooklm.log public shims
# (intentionally left blank; PR-D appends below this marker)
# ---------------------------------------------------------------------------
