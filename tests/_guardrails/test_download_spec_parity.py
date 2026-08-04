"""The three ``DownloadTypeSpec`` registries must not drift apart.

The CLI (``cli/_download_specs.py``), the MCP Studio tools
(``mcp/tools/_studio_download.py``), and the REST server
(``server/routes/artifacts.py``) each rebuild the SAME download-type table from
the neutral ``_app.download`` types — deliberately, so neither adapter imports
another's Click/FastMCP-coupled module. The cost of that deliberate duplication
is that a per-type fix applied to one copy silently leaves the other two wrong.

That is exactly how #2034 shipped: audio was registered as ``.mp3`` in all three
tables while the artifact metadata advertises ``audio/mp4``, so every surface
wrote AAC/MP4 bytes under an MP3 name. These tests pin the registries together
on every field that decides what lands on disk, and pin every registered
extension into the one shared MIME table, so a future one-surface edit fails
loudly instead of shipping a partial fix.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._app import download as download_core
from notebooklm.cli._download_specs import DOWNLOAD_SPECS_BY_NAME

pytest.importorskip("fastmcp", reason="MCP registry needs the 'mcp' extra")
pytest.importorskip("fastapi", reason="REST registry needs the 'server' extra")

from notebooklm.mcp.tools._studio_download import (  # noqa: E402 - after importorskip
    _DOWNLOAD_SPECS as MCP_SPECS,
)
from notebooklm.server.routes.artifacts import DOWNLOAD_SPECS as SERVER_SPECS  # noqa: E402

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


def test_registries_cover_the_same_download_types() -> None:
    """All three tables register the same set of download-type keys."""
    cli_names = set(DOWNLOAD_SPECS_BY_NAME)
    assert set(MCP_SPECS) == cli_names, (
        "mcp/tools/_studio_download.py::_DOWNLOAD_SPECS drifted from "
        "cli/_download_specs.py::DOWNLOAD_SPECS: "
        f"mcp-only={sorted(set(MCP_SPECS) - cli_names)} "
        f"cli-only={sorted(cli_names - set(MCP_SPECS))}"
    )
    assert set(SERVER_SPECS) == cli_names, (
        "server/routes/artifacts.py::DOWNLOAD_SPECS drifted from "
        "cli/_download_specs.py::DOWNLOAD_SPECS: "
        f"server-only={sorted(set(SERVER_SPECS) - cli_names)} "
        f"cli-only={sorted(cli_names - set(SERVER_SPECS))}"
    )


@pytest.mark.parametrize("name", sorted(DOWNLOAD_SPECS_BY_NAME))
def test_registries_agree_on_behavioural_fields(name: str) -> None:
    """Each type's kind / extension / download binding / format axis match everywhere."""
    cli_behaviour = _behaviour(DOWNLOAD_SPECS_BY_NAME[name])
    for label, table in (
        ("mcp/tools/_studio_download.py::_DOWNLOAD_SPECS", MCP_SPECS),
        ("server/routes/artifacts.py::DOWNLOAD_SPECS", SERVER_SPECS),
    ):
        assert _behaviour(table[name]) == cli_behaviour, (
            f"{label} row {name!r} disagrees with cli/_download_specs.py — a "
            "per-type change must be applied to all three registries"
        )


def test_audio_downloads_are_labelled_m4a() -> None:
    """Audio Overviews are AAC in an MP4 container, so ``.m4a`` / ``audio/mp4`` (#2034).

    Pinned explicitly (not just cross-registry) because ``.mp3`` looks plausible
    and passes every other test: the mislabel is only visible against the media
    bytes, which no unit test downloads.
    """
    for label, spec in (
        ("cli", DOWNLOAD_SPECS_BY_NAME["audio"]),
        ("mcp", MCP_SPECS["audio"]),
        ("server", SERVER_SPECS["audio"]),
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
