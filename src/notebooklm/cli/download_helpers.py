"""Helper functions for download commands.

The transport-neutral pieces (``ArtifactDict``, :func:`select_artifact`,
:func:`artifact_title_to_filename`, and the ``DUPLICATE_SUFFIX_RESERVE``
constant) now live in :mod:`notebooklm._app.download` so the shared download
core can use them without crossing back into the Click-coupled CLI layer. They
are re-exported here unchanged so the historical
``from notebooklm.cli.download_helpers import ...`` import surface keeps
resolving.

:func:`resolve_partial_artifact_id` stays here because it depends on the
Click-coupled :mod:`notebooklm.cli.resolve` matching core and re-shapes its
error wording to the download command's historical text.
"""

from .._app.download import (
    DUPLICATE_SUFFIX_RESERVE,
    ArtifactDict,
    artifact_title_to_filename,
    select_artifact,
)
from .resolve import resolve_partial_id_in_items

__all__ = [
    "DUPLICATE_SUFFIX_RESERVE",
    "ArtifactDict",
    "artifact_title_to_filename",
    "resolve_partial_artifact_id",
    "select_artifact",
]


def resolve_partial_artifact_id(artifacts: list[ArtifactDict], artifact_id: str) -> str:
    """Resolve a partial artifact ID to a full ID.

    UUID-shaped IDs (canonical 8-4-4-4-12 hex layout, case-insensitive -
    see :data:`notebooklm.cli.resolve.FULL_ID_PATTERN`) are validated against
    the pre-fetched ``artifacts`` list (full-ID passthrough is disabled for
    this path via ``allow_full_id_passthrough=False``). A UUID not present
    in ``artifacts`` raises the canonical local "not found" error instead
    of passing through to a backend 404. Anything else - including a 25-char
    prefix of a 36-char UUID - is matched as a case-insensitive prefix
    against the artifact list, so unique prefixes resolve locally rather
    than reaching the backend as truncated IDs.

    The matching logic is delegated to
    :func:`notebooklm.cli.resolve.resolve_partial_id_in_items` (the canonical
    sync core shared with the async resolver), supplied with the dict-shaped
    accessor pair (the download path stores artifacts as
    :class:`ArtifactDict`, not the dataclass shape the async resolver
    consumes) and the ``ValueError`` factory (the download command body
    catches ``ValueError`` and converts to an error envelope, so we keep
    the historical exception type rather than ``click.ClickException``).

    The "no match" / "ambiguous" message text retains its historical wording
    rather than the canonical resolver's wording - the download command's
    user-visible error envelope text predates the resolver consolidation
    and a customer or external test could rely on it. The translation costs about
    8 lines and keeps the user contract verbatim. Successful partial matches
    also remain silent, matching the historical download helper behavior.

    Args:
        artifacts: Pre-fetched list of artifacts to search.
        artifact_id: Full or partial artifact ID.

    Returns:
        Full artifact ID.

    Raises:
        ValueError: If no match found or prefix is ambiguous.
    """
    try:
        return resolve_partial_id_in_items(
            artifact_id,
            list(artifacts),
            entity_name="artifact",
            list_command="artifact list",
            id_of=lambda a: a["id"],
            title_of=lambda a: a["title"],
            error_factory=ValueError,
            emit_match_status=False,
            allow_full_id_passthrough=False,
        )
    except ValueError as e:
        # Re-shape the canonical "No artifact found starting with..." /
        # "Ambiguous ID 'X' matches N artifacts:\n..." wording to the
        # historical download-helpers wording. Both messages remain valid
        # ValueError instances; only the human-readable string changes.
        msg = str(e)
        if msg.startswith("No artifact found starting with"):
            raise ValueError(f"Artifact '{artifact_id}' not found") from e
        if msg.startswith("Ambiguous ID"):
            partial = artifact_id.strip().lower()
            matches = [a for a in artifacts if a["id"].lower().startswith(partial)]
            options = ", ".join(f"{a['id']} ({a['title']})" for a in matches)
            raise ValueError(f"Ambiguous partial ID '{artifact_id}' matches: {options}") from e
        raise
