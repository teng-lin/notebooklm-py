"""All download surfaces derive from one neutral registry.

``_app.download_specs.DOWNLOAD_REGISTRY`` is the only table that states an
artifact representation's extension, MIME, download binding, and format axis.
MCP and REST consume its immutable runtime projection directly; CLI copies the
projection only to add Click help and the legacy ``slide_format`` parameter.

These architecture tests ensure the adapters cannot return to the triplicated
tables that allowed #2034's ``.mp3``/``audio/mp4`` mismatch.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from notebooklm._app import download as download_core
from notebooklm._app import download_specs as download_specs_core
from notebooklm.cli._download_specs import DOWNLOAD_SPECS_BY_NAME
from notebooklm.cli.download_cmd import download as download_group

#: Spec fields that determine the bytes/filename a download produces.
#:
#: Deliberately excluded — these are adapter-local, not behavioural:
#:
#: * ``help_summary`` / ``help_examples`` — CLI ``--help`` prose; the neutral
#:   registries leave them empty by design.
#: * ``format_param_name`` — the name of the *adapter's own* kwarg carrying the
#:   format choice. The CLI's slide-deck row keeps the legacy Click param name
#:   ``slide_format`` while MCP/REST use the default ``output_format``; both
#:   resolve to the same ``format_kwarg`` on the client call.
_BEHAVIOURAL_FIELDS = (
    "name",
    "kind",
    "extension",
    "default_dir",
    "download_attr",
    "format_choices",
    "format_default",
    "format_extension_map",
    "format_kwarg",
    "forward_format_only_if_set",
)


def _behaviour(spec: download_core.DownloadTypeSpec) -> dict[str, Any]:
    return {field: getattr(spec, field) for field in _BEHAVIOURAL_FIELDS}


def _reachable_extensions() -> set[str]:
    """Every extension a registered spec can resolve to — default and per-format."""
    extensions: set[str] = set()
    for spec in DOWNLOAD_SPECS_BY_NAME.values():
        extensions.add(spec.extension)
        extensions.update(spec.format_extension_map.values())
    return extensions


def test_non_cli_adapters_use_the_shared_projection_directly() -> None:
    """MCP and REST cannot mutate or restate a behavioural field locally."""
    pytest.importorskip("fastmcp", reason="MCP registry needs the 'mcp' extra")
    pytest.importorskip("fastapi", reason="REST registry needs the 'server' extra")

    from notebooklm.mcp.tools._studio_download import _DOWNLOAD_SPECS as mcp_specs
    from notebooklm.server.routes.artifacts import DOWNLOAD_SPECS as server_specs

    shared = download_specs_core.DOWNLOAD_SPECS_BY_NAME
    assert mcp_specs is shared
    assert server_specs is shared


@pytest.mark.parametrize("name", sorted(DOWNLOAD_SPECS_BY_NAME))
def test_cli_adds_only_adapter_local_fields(name: str) -> None:
    """Click help/param residue does not alter shared download behaviour."""
    shared = download_specs_core.DOWNLOAD_SPECS_BY_NAME[name]
    cli = DOWNLOAD_SPECS_BY_NAME[name]
    assert _behaviour(cli) == _behaviour(shared)
    assert cli.help_summary
    assert cli.help_examples


def test_format_extensions_and_mime_are_derived_from_the_same_rows() -> None:
    """Every descriptor projects to both the runtime format map and MIME lookup."""
    for entry in download_specs_core.DOWNLOAD_REGISTRY:
        runtime = download_specs_core.DOWNLOAD_SPECS_BY_NAME[entry.name]
        assert runtime.extension == entry.default_output.extension
        assert (
            download_core.mime_type_for_extension(runtime.extension)
            == entry.default_output.mime_type
        )
        assert runtime.format_choices == tuple(name for name, _format in entry.formats)
        for name, format_spec in entry.formats:
            assert runtime.format_extension_map[name] == format_spec.extension
            assert (
                download_core.mime_type_for_extension(format_spec.extension)
                == format_spec.mime_type
            )


def test_legacy_format_projection_accepts_a_one_row_format_addition() -> None:
    """A new format on one contributing row needs no second registry edit."""
    quiz = next(entry for entry in download_specs_core.DOWNLOAD_REGISTRY if entry.name == "quiz")
    text_format = download_specs_core.DownloadFormatSpec(".txt", "text/plain")
    extended_quiz = replace(
        quiz,
        alternate_formats=(*quiz.alternate_formats, ("text", text_format)),
    )
    registry = tuple(
        extended_quiz if entry is quiz else entry for entry in download_specs_core.DOWNLOAD_REGISTRY
    )

    projected = download_specs_core._build_legacy_format_extensions(registry)

    assert projected["text"] == ".txt"


def test_legacy_format_projection_rejects_conflicting_extensions() -> None:
    """Contributing rows cannot assign two extensions to one format name."""
    flashcards = next(
        entry for entry in download_specs_core.DOWNLOAD_REGISTRY if entry.name == "flashcards"
    )
    conflicting_markdown = download_specs_core.DownloadFormatSpec(".mkdn", "text/markdown")
    conflicting_flashcards = replace(
        flashcards,
        alternate_formats=(
            ("markdown", conflicting_markdown),
            *flashcards.alternate_formats[1:],
        ),
    )
    registry = tuple(
        conflicting_flashcards if entry is flashcards else entry
        for entry in download_specs_core.DOWNLOAD_REGISTRY
    )

    with pytest.raises(ValueError, match="conflicting legacy extensions for 'markdown'"):
        download_specs_core._build_legacy_format_extensions(registry)


def test_download_format_extensions_must_be_lowercase() -> None:
    """Registry keys match the lowercase normalization used by MIME lookup."""
    with pytest.raises(ValueError, match="extension must be lowercase"):
        download_specs_core.DownloadFormatSpec(".PDF", "application/pdf")


def test_click_uses_the_registry_projected_legacy_slide_parameter() -> None:
    """The real Click option preserves ``slide_format`` from the CLI projection."""
    command = download_group.commands["slide-deck"]
    format_option = next(
        param for param in command.params if "--format" in getattr(param, "opts", ())
    )
    assert format_option.name == DOWNLOAD_SPECS_BY_NAME["slide-deck"].format_param_name
    assert format_option.name == "slide_format"


def test_audio_downloads_are_labelled_m4a() -> None:
    """Audio Overviews are AAC in an MP4 container, so ``.m4a`` / ``audio/mp4`` (#2034).

    Pinned explicitly (not just cross-registry) because ``.mp3`` looks plausible
    and passes every other test: the mislabel is only visible against the media
    bytes, which no unit test downloads.
    """
    for label, spec in (
        ("shared", download_specs_core.DOWNLOAD_SPECS_BY_NAME["audio"]),
        ("cli", DOWNLOAD_SPECS_BY_NAME["audio"]),
    ):
        assert spec.extension == ".m4a", f"{label} audio spec regressed to {spec.extension!r}"
    assert download_core.mime_type_for_extension(".m4a") == "audio/mp4"


def test_every_registered_extension_has_a_mime_type() -> None:
    """No spec can resolve to an extension the shared MIME table doesn't know.

    An unmapped extension would silently degrade to ``application/octet-stream``
    on the MCP link payload / ``/files/dl`` route and the REST ``/download``
    response.
    """
    unmapped = sorted(
        ext for ext in _reachable_extensions() if ext not in download_core.EXTENSION_MIME_TYPES
    )
    assert unmapped == [], (
        f"download extensions missing from _app.download.EXTENSION_MIME_TYPES: {unmapped}"
    )


def test_mime_table_has_no_unreachable_rows() -> None:
    """The MIME table carries no extension no spec can produce.

    A stale row is how a corrected mapping drifts back: ``.mp3 -> audio/mpeg``
    survived in the MCP table long enough to be re-adopted.
    """
    reachable = _reachable_extensions()
    stale = sorted(ext for ext in download_core.EXTENSION_MIME_TYPES if ext not in reachable)
    assert stale == [], f"unreachable rows in _app.download.EXTENSION_MIME_TYPES: {stale}"
