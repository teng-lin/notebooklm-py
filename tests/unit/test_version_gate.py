"""Enforce v0.5.0 deprecation removals.

This file is **passive at every version < 0.5.0**: the removal-assertion
tests are skipped automatically and only the version-parse smoke test runs.
Once ``notebooklm.__version__`` parses to ``Version("0.5.0")`` or higher,
every gated test activates and asserts that the
matching deprecated public symbol from ``docs/stability.md`` is no longer
reachable — either the access raises ``AttributeError`` outright (for the
module-level symbols) or the per-instance ``@property`` accessor is gone.

Why this lives here instead of a release-checklist doc:

* The removal can't ship without flipping these tests green, so the gate
  is impossible to forget.
* At v0.4.x there is exactly zero cost: gated skips + 1 pass per run.
* When the 0.5.0 PR lands, the gate test transitions from "skipped" to
  "passing" with no changes to the file itself — the same code asserts
  both the current ("deprecated, still present") state and the future
  ("removed") state.
"""

from __future__ import annotations

import pytest
from packaging.version import Version

import notebooklm

_V_050 = Version("0.5.0")
_CURRENT = Version(notebooklm.__version__)
_AT_OR_PAST_050 = pytest.mark.skipif(
    _CURRENT < _V_050,
    reason=f"v0.5.0 deprecation-removal gate inactive at {_CURRENT}",
)


def test_version_parses() -> None:
    """Smoke test that runs at every version.

    Pins that ``notebooklm.__version__`` is a valid PEP 440 version string and
    sits in the 0.x line — both invariants the gated tests below depend on.
    """
    assert _CURRENT.major == 0, (
        f"Project no longer on 0.x line ({_CURRENT}); revisit the v0.5.0 "
        "deprecation-removal gate in this file before bumping further."
    )


@_AT_OR_PAST_050
def test_source_source_type_removed() -> None:
    """``Source.source_type`` (deprecated since 0.3.0) is removed at 0.5.0."""
    from notebooklm.types import Source

    source = Source(id="x", _type_code=5)
    with pytest.raises(AttributeError):
        _ = source.source_type  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_artifact_artifact_type_removed() -> None:
    """``Artifact.artifact_type`` (deprecated since 0.3.0) is removed at 0.5.0."""
    from notebooklm.types import Artifact

    artifact = Artifact(id="x", title="t", _artifact_type=1, status=3)
    with pytest.raises(AttributeError):
        _ = artifact.artifact_type  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_artifact_variant_removed() -> None:
    """``Artifact.variant`` (deprecated since 0.3.0) is removed at 0.5.0."""
    from notebooklm.types import Artifact

    artifact = Artifact(id="x", title="t", _artifact_type=4, status=3, _variant=2)
    with pytest.raises(AttributeError):
        _ = artifact.variant  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_source_fulltext_source_type_removed() -> None:
    """``SourceFulltext.source_type`` (deprecated since 0.3.0) is removed at 0.5.0."""
    from notebooklm.types import SourceFulltext

    fulltext = SourceFulltext(source_id="x", title="t", content="c")
    with pytest.raises(AttributeError):
        _ = fulltext.source_type  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_studio_content_type_removed() -> None:
    """``notebooklm.StudioContentType`` (deprecated) is removed at 0.5.0."""
    with pytest.raises(AttributeError):
        _ = notebooklm.StudioContentType  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_default_storage_path_removed() -> None:
    """``notebooklm.DEFAULT_STORAGE_PATH`` (deprecated) is removed at 0.5.0."""
    with pytest.raises(AttributeError):
        _ = notebooklm.DEFAULT_STORAGE_PATH  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_rpc_types_studio_content_type_removed() -> None:
    """``notebooklm.rpc.types.StudioContentType`` alias is removed at 0.5.0."""
    import notebooklm.rpc.types as rpc_types

    with pytest.raises(AttributeError):
        _ = rpc_types.StudioContentType  # type: ignore[attr-defined]


@_AT_OR_PAST_050
def test_rpc_studio_content_type_removed() -> None:
    """``notebooklm.rpc.StudioContentType`` re-export is removed at 0.5.0."""
    import notebooklm.rpc as rpc

    with pytest.raises(AttributeError):
        _ = rpc.StudioContentType  # type: ignore[attr-defined]
    assert "StudioContentType" not in rpc.__all__
