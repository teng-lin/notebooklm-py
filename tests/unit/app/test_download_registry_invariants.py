"""Invariant guards on the canonical download registry.

The registry itself is well-formed, so the ``__post_init__`` and builder
validations never fire in production. These cases construct malformed rows to
prove each guard bites — an unenforced invariant is a comment, and this table
is the single source of truth for every artifact's extension and MIME type.
"""

from __future__ import annotations

import pytest

from notebooklm._app import download_specs
from notebooklm._app.download_specs import (
    DOWNLOAD_REGISTRY,
    DownloadFormatSpec,
    DownloadRegistryEntry,
)
from notebooklm.types import ArtifactType

PDF = DownloadFormatSpec(".pdf", "application/pdf")
PPTX = DownloadFormatSpec(
    ".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


def _entry(**overrides: object) -> DownloadRegistryEntry:
    fields: dict[str, object] = {
        "name": "slide-deck",
        "kind": ArtifactType.SLIDE_DECK,
        "default_output": PDF,
        "default_dir": "./slide-decks",
        "download_attr": "download_slide_deck",
    }
    fields.update(overrides)
    return DownloadRegistryEntry(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DownloadFormatSpec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extension", "mime_type", "message"),
    [
        pytest.param("pdf", "application/pdf", "must start with '.'", id="no-leading-dot"),
        pytest.param(".PDF", "application/pdf", "must be lowercase", id="uppercase"),
        pytest.param(".pdf", "pdf", "must contain '/'", id="not-a-mime-type"),
    ],
)
def test_format_spec_rejects_malformed_descriptors(
    extension: str, mime_type: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DownloadFormatSpec(extension, mime_type)


# ---------------------------------------------------------------------------
# DownloadRegistryEntry
# ---------------------------------------------------------------------------


def test_entry_rejects_duplicate_alternate_format_names() -> None:
    with pytest.raises(ValueError, match="duplicate formats"):
        _entry(
            format_default="pdf",
            alternate_formats=(("pptx", PPTX), ("pptx", PPTX)),
            format_kwarg="output_format",
        )


def test_entry_rejects_a_default_repeated_among_the_alternates() -> None:
    with pytest.raises(ValueError, match="is repeated for"):
        _entry(
            format_default="pdf",
            alternate_formats=(("pdf", PDF),),
            format_kwarg="output_format",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"format_default": "pdf", "format_kwarg": "output_format"}, id="no-alternates"
        ),
        pytest.param(
            {"alternate_formats": (("pptx", PPTX),), "format_kwarg": "output_format"},
            id="no-default",
        ),
    ],
)
def test_entry_requires_a_default_and_alternates_together(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must define a default and alternate formats together"):
        _entry(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"format_default": "pdf", "alternate_formats": (("pptx", PPTX),)},
            id="formats-without-kwarg",
        ),
        pytest.param({"format_kwarg": "output_format"}, id="kwarg-without-formats"),
    ],
)
def test_entry_requires_a_format_kwarg_with_its_formats(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must define its format kwarg with its formats"):
        _entry(**overrides)


def test_a_format_less_entry_cannot_contribute_to_the_legacy_map() -> None:
    with pytest.raises(ValueError, match="contributes to the legacy format map"):
        _entry(contributes_to_legacy_format_extensions=True)


def test_formats_places_the_default_first() -> None:
    entry = _entry(
        format_default="pdf",
        alternate_formats=(("pptx", PPTX),),
        format_kwarg="output_format",
    )

    assert entry.formats == (("pdf", PDF), ("pptx", PPTX))


def test_a_format_less_entry_exposes_no_format_axis() -> None:
    entry = _entry()

    assert entry.formats == ()
    spec = entry.to_download_type_spec()
    assert spec.format_choices == ()
    assert dict(spec.format_extension_map) == {}
    assert spec.extension == ".pdf"


# ---------------------------------------------------------------------------
# Registry-wide builders
# ---------------------------------------------------------------------------


def test_duplicate_names_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download_specs, "DOWNLOAD_REGISTRY", (_entry(), _entry()))

    with pytest.raises(ValueError, match="duplicate names"):
        download_specs._build_download_specs()


def test_duplicate_artifact_kinds_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        download_specs,
        "DOWNLOAD_REGISTRY",
        (_entry(), _entry(name="slide-deck-copy", download_attr="download_slide_deck_copy")),
    )

    with pytest.raises(ValueError, match="duplicate artifact kinds"):
        download_specs._build_download_specs()


def test_one_extension_may_not_carry_two_mime_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        download_specs,
        "DOWNLOAD_REGISTRY",
        (
            _entry(),
            _entry(
                name="report",
                kind=ArtifactType.REPORT,
                default_output=DownloadFormatSpec(".pdf", "text/pdf"),
                download_attr="download_report",
            ),
        ),
    )

    with pytest.raises(ValueError, match="conflicting MIME types"):
        download_specs._build_extension_mime_types()


def test_legacy_format_map_rejects_conflicting_extensions() -> None:
    entries = (
        _entry(
            format_default="pdf",
            alternate_formats=(("pptx", PPTX),),
            format_kwarg="output_format",
            contributes_to_legacy_format_extensions=True,
        ),
        _entry(
            name="report",
            kind=ArtifactType.REPORT,
            download_attr="download_report",
            format_default="pdf",
            alternate_formats=(("md", DownloadFormatSpec(".md", "text/markdown")),),
            format_kwarg="output_format",
            contributes_to_legacy_format_extensions=True,
            default_output=DownloadFormatSpec(".docx", "application/msword"),
        ),
    )

    with pytest.raises(ValueError, match="conflicting legacy extensions"):
        download_specs._build_legacy_format_extensions(entries)


def test_legacy_format_map_requires_at_least_one_contributor() -> None:
    with pytest.raises(ValueError, match="no legacy format-map contributors"):
        download_specs._build_legacy_format_extensions((_entry(),))


def test_the_shipped_registry_satisfies_every_builder() -> None:
    """The guards above only matter if the real table still passes them."""
    specs = download_specs._build_download_specs()

    assert set(specs) == {entry.name for entry in DOWNLOAD_REGISTRY}
    assert download_specs._build_extension_mime_types()[".m4a"] == "audio/mp4"
    assert download_specs._build_legacy_format_extensions(DOWNLOAD_REGISTRY)
