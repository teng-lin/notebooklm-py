"""Canonical registry for artifact download types and formats.

This module is the one transport-neutral source of truth for every downloadable
artifact representation. Each format descriptor keeps its extension and MIME
type together; the runtime ``DownloadTypeSpec`` rows, the format enums exposed
by adapters, and the extension-to-MIME lookup are all derived from that table.

Adapters may project local presentation fields (for example CLI help text or
the legacy ``slide_format`` Click parameter name), but must not restate file
extensions, MIME types, download bindings, or legal format values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ..types import ArtifactType

__all__ = [
    "DOWNLOAD_FORMAT_NAMES",
    "DOWNLOAD_REGISTRY",
    "DOWNLOAD_SPECS_BY_NAME",
    "EXTENSION_MIME_TYPES",
    "FORMAT_EXTENSIONS",
    "DownloadFormatSpec",
    "DownloadRegistryEntry",
    "DownloadTypeSpec",
]


@dataclass(frozen=True)
class DownloadTypeSpec:
    """Runtime metadata consumed by the shared download core and adapters.

    The neutral registry below owns every behavioural field. ``help_*`` and
    ``format_param_name`` remain here because the CLI projects its local
    presentation and compatibility residue onto otherwise shared rows.
    """

    name: str
    kind: ArtifactType
    extension: str
    default_dir: str
    download_attr: str
    help_summary: str
    help_examples: str
    format_choices: tuple[str, ...] = ()
    format_default: str = ""
    format_help: str = ""
    format_extension_map: Mapping[str, str] = field(default_factory=dict)
    format_kwarg: str = ""
    format_param_name: str = "output_format"
    forward_format_only_if_set: bool = False


@dataclass(frozen=True)
class DownloadFormatSpec:
    """One file representation, keeping its extension and MIME inseparable."""

    extension: str
    mime_type: str

    def __post_init__(self) -> None:
        if not self.extension.startswith("."):
            raise ValueError(f"download extension must start with '.': {self.extension!r}")
        if self.extension != self.extension.lower():
            raise ValueError(f"download extension must be lowercase: {self.extension!r}")
        if "/" not in self.mime_type:
            raise ValueError(f"download MIME type must contain '/': {self.mime_type!r}")


@dataclass(frozen=True)
class DownloadRegistryEntry:
    """Canonical metadata for one downloadable artifact type.

    ``default_output`` is always present. Types with a public format axis name
    that representation in ``format_default`` and list only the alternatives in
    ``alternate_formats``; projection reconstructs the complete ordered choice
    and extension maps without repeating the default representation.
    """

    name: str
    kind: ArtifactType
    default_output: DownloadFormatSpec
    default_dir: str
    download_attr: str
    format_default: str = ""
    alternate_formats: tuple[tuple[str, DownloadFormatSpec], ...] = ()
    format_kwarg: str = ""
    forward_format_only_if_set: bool = False
    contributes_to_legacy_format_extensions: bool = False

    def __post_init__(self) -> None:
        alternate_names = [name for name, _format in self.alternate_formats]
        if len(alternate_names) != len(set(alternate_names)):
            raise ValueError(f"duplicate formats for download type {self.name!r}")
        if self.format_default in alternate_names:
            raise ValueError(
                f"default format {self.format_default!r} is repeated for {self.name!r}"
            )
        has_format_axis = bool(self.format_default)
        if has_format_axis != bool(self.alternate_formats):
            raise ValueError(
                f"download type {self.name!r} must define a default and alternate formats together"
            )
        if has_format_axis != bool(self.format_kwarg):
            raise ValueError(
                f"download type {self.name!r} must define its format kwarg with its formats"
            )
        if self.contributes_to_legacy_format_extensions and not has_format_axis:
            raise ValueError(
                f"download type {self.name!r} contributes to the legacy format map "
                "but defines no formats"
            )

    @property
    def formats(self) -> tuple[tuple[str, DownloadFormatSpec], ...]:
        """All selectable formats in stable schema/display order."""
        if not self.format_default:
            return ()
        return ((self.format_default, self.default_output), *self.alternate_formats)

    def to_download_type_spec(
        self,
        *,
        help_summary: str = "",
        help_examples: str = "",
        format_help: str = "",
        format_param_name: str = "output_format",
    ) -> DownloadTypeSpec:
        """Project the neutral row, adding only adapter-local presentation fields."""
        return DownloadTypeSpec(
            name=self.name,
            kind=self.kind,
            extension=self.default_output.extension,
            default_dir=self.default_dir,
            download_attr=self.download_attr,
            help_summary=help_summary,
            help_examples=help_examples,
            format_choices=tuple(name for name, _format in self.formats),
            format_default=self.format_default,
            format_help=format_help,
            format_extension_map=MappingProxyType(
                {name: format_spec.extension for name, format_spec in self.formats}
            ),
            format_kwarg=self.format_kwarg,
            format_param_name=format_param_name,
            forward_format_only_if_set=self.forward_format_only_if_set,
        )


DOWNLOAD_REGISTRY: tuple[DownloadRegistryEntry, ...] = (
    DownloadRegistryEntry(
        name="audio",
        kind=ArtifactType.AUDIO,
        # Audio Overview bytes are AAC in an MP4 container, not MP3 (#2034).
        # Keeping MIME beside the extension makes that invariant indivisible.
        default_output=DownloadFormatSpec(".m4a", "audio/mp4"),
        default_dir="./audio",
        download_attr="download_audio",
    ),
    DownloadRegistryEntry(
        name="video",
        kind=ArtifactType.VIDEO,
        default_output=DownloadFormatSpec(".mp4", "video/mp4"),
        default_dir="./video",
        download_attr="download_video",
    ),
    DownloadRegistryEntry(
        name="slide-deck",
        kind=ArtifactType.SLIDE_DECK,
        default_output=DownloadFormatSpec(".pdf", "application/pdf"),
        default_dir="./slide-decks",
        download_attr="download_slide_deck",
        format_default="pdf",
        alternate_formats=(
            (
                "pptx",
                DownloadFormatSpec(
                    ".pptx",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                ),
            ),
        ),
        format_kwarg="output_format",
        forward_format_only_if_set=True,
    ),
    DownloadRegistryEntry(
        name="infographic",
        kind=ArtifactType.INFOGRAPHIC,
        default_output=DownloadFormatSpec(".png", "image/png"),
        default_dir="./infographic",
        download_attr="download_infographic",
    ),
    DownloadRegistryEntry(
        name="report",
        kind=ArtifactType.REPORT,
        default_output=DownloadFormatSpec(".md", "text/markdown"),
        default_dir="./reports",
        download_attr="download_report",
    ),
    DownloadRegistryEntry(
        name="mind-map",
        kind=ArtifactType.MIND_MAP,
        default_output=DownloadFormatSpec(".json", "application/json"),
        default_dir="./mind-maps",
        download_attr="download_mind_map",
    ),
    DownloadRegistryEntry(
        name="data-table",
        kind=ArtifactType.DATA_TABLE,
        default_output=DownloadFormatSpec(".csv", "text/csv"),
        default_dir="./data-tables",
        download_attr="download_data_table",
    ),
    DownloadRegistryEntry(
        name="quiz",
        kind=ArtifactType.QUIZ,
        default_output=DownloadFormatSpec(".json", "application/json"),
        default_dir="./quizzes",
        download_attr="download_quiz",
        format_default="json",
        alternate_formats=(
            ("markdown", DownloadFormatSpec(".md", "text/markdown")),
            ("html", DownloadFormatSpec(".html", "text/html")),
        ),
        format_kwarg="output_format",
        contributes_to_legacy_format_extensions=True,
    ),
    DownloadRegistryEntry(
        name="flashcards",
        kind=ArtifactType.FLASHCARDS,
        default_output=DownloadFormatSpec(".json", "application/json"),
        default_dir="./flashcards",
        download_attr="download_flashcards",
        format_default="json",
        alternate_formats=(
            ("markdown", DownloadFormatSpec(".md", "text/markdown")),
            ("html", DownloadFormatSpec(".html", "text/html")),
        ),
        format_kwarg="output_format",
        contributes_to_legacy_format_extensions=True,
    ),
)


def _build_download_specs() -> Mapping[str, DownloadTypeSpec]:
    specs = {entry.name: entry.to_download_type_spec() for entry in DOWNLOAD_REGISTRY}
    if len(specs) != len(DOWNLOAD_REGISTRY):
        raise ValueError("duplicate names in DOWNLOAD_REGISTRY")
    kinds = [entry.kind for entry in DOWNLOAD_REGISTRY]
    if len(kinds) != len(set(kinds)):
        raise ValueError("duplicate artifact kinds in DOWNLOAD_REGISTRY")
    return MappingProxyType(specs)


def _build_extension_mime_types() -> dict[str, str]:
    mime_types: dict[str, str] = {}
    for entry in DOWNLOAD_REGISTRY:
        outputs = (
            entry.default_output,
            *(format_spec for _, format_spec in entry.alternate_formats),
        )
        for output in outputs:
            existing = mime_types.setdefault(output.extension, output.mime_type)
            if existing != output.mime_type:
                raise ValueError(
                    f"conflicting MIME types for {output.extension!r}: "
                    f"{existing!r} and {output.mime_type!r}"
                )
    return mime_types


def _build_legacy_format_extensions(
    entries: Iterable[DownloadRegistryEntry],
) -> dict[str, str]:
    """Project the explicitly marked rows into the legacy format map.

    ``FORMAT_EXTENSIONS`` predates the per-type registry and historically
    represents the union of quiz/flashcard choices. Registry metadata marks
    contributors without coupling this projection to artifact names. A format
    added to one contributor is included automatically; incompatible duplicate
    format names fail loudly.
    """
    extensions: dict[str, str] = {}
    contributor_count = 0
    for entry in entries:
        if not entry.contributes_to_legacy_format_extensions:
            continue
        contributor_count += 1
        for format_name, format_spec in entry.formats:
            existing = extensions.setdefault(format_name, format_spec.extension)
            if existing != format_spec.extension:
                raise ValueError(
                    f"conflicting legacy extensions for {format_name!r}: "
                    f"{existing!r} and {format_spec.extension!r}"
                )
    if not contributor_count:
        raise ValueError("DOWNLOAD_REGISTRY has no legacy format-map contributors")
    return extensions


#: Shared runtime projections consumed directly by MCP and REST. The mapping is
#: immutable so an adapter cannot mutate the canonical rows at import time.
DOWNLOAD_SPECS_BY_NAME: Mapping[str, DownloadTypeSpec] = _build_download_specs()

#: Compatibility projection used by the CLI service and older callers. Its
#: contributing rows are marked in the registry, so names and formats stay local
#: to the single canonical table.
FORMAT_EXTENSIONS: Mapping[str, str] = MappingProxyType(
    _build_legacy_format_extensions(DOWNLOAD_REGISTRY)
)

#: Extension-to-MIME lookup derived solely from :data:`DOWNLOAD_REGISTRY`.
EXTENSION_MIME_TYPES: Mapping[str, str] = MappingProxyType(_build_extension_mime_types())

#: Stable union used to derive adapter schemas without restating legal values.
DOWNLOAD_FORMAT_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        format_name for entry in DOWNLOAD_REGISTRY for format_name, _format_spec in entry.formats
    )
)
