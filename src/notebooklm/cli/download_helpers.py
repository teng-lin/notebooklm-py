"""Helper functions for download commands."""

import re
from typing import TypedDict

from .resolve import resolve_partial_id_in_items

# Reserve space for " (999)" suffix when handling duplicate filenames
DUPLICATE_SUFFIX_RESERVE = 7


class ArtifactDict(TypedDict):
    """Artifact structure returned by list_artifacts API."""

    id: str
    title: str
    created_at: int  # Unix timestamp


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


def select_artifact(
    artifacts: list[ArtifactDict],
    latest: bool = True,
    earliest: bool = False,
    name: str | None = None,
    artifact_id: str | None = None,
) -> tuple[ArtifactDict, str]:
    """
    Select an artifact from a list based on criteria.

    CRITICAL: Implements Filter -> Count -> Select logic:
    1. Filter artifacts by name/artifact_id if provided
    2. Count matches (0/1/many)
    3. Apply latest/earliest to remaining matches

    Args:
        artifacts: List of artifact dicts with 'id', 'title', 'created_at'
        latest: Select most recent (default: True)
        earliest: Select oldest (overrides latest if True)
        name: Filter by title (case-insensitive substring match)
        artifact_id: Select by exact artifact ID

    Returns:
        Tuple of (selected_artifact, selection_reason)

    Raises:
        ValueError: If no match, invalid criteria, or both latest+earliest
    """
    # Validate inputs
    if not artifacts:
        raise ValueError("No artifacts found")

    if latest and earliest:
        raise ValueError("Cannot specify both --latest and --earliest")

    # Step 1: Filter
    filtered = artifacts

    if artifact_id:
        filtered = [a for a in artifacts if a["id"] == artifact_id]
        if not filtered:
            raise ValueError(f"Artifact {artifact_id} not found")
        return filtered[0], f"matched by ID: {artifact_id}"

    if name:
        name_lower = name.lower()
        filtered = [a for a in artifacts if name_lower in a["title"].lower()]
        if not filtered:
            raise ValueError(
                f"No artifacts matching '{name}'. "
                f"Available: {', '.join(a['title'] for a in artifacts)}"
            )

    # Step 2: Count
    count = len(filtered)

    # Step 3: Select
    if count == 1:
        reason = "matched by name" if name else "only artifact"
        return filtered[0], reason

    # Multiple matches - apply latest/earliest
    if earliest:
        selected = min(filtered, key=lambda a: a["created_at"])
        return selected, f"earliest of {count} artifacts"
    else:
        # Default to latest (latest=True by default)
        selected = max(filtered, key=lambda a: a["created_at"])
        return selected, f"latest of {count} artifacts"


def artifact_title_to_filename(
    title: str,
    extension: str,
    existing_files: set[str],
    max_length: int = 240,  # Leave room for extension and (N) suffix
) -> str:
    """
    Convert artifact title to safe filename.

    Args:
        title: Artifact title
        extension: File extension (with leading dot, e.g., ".mp3")
        existing_files: Set of filenames already used
        max_length: Maximum filename length before extension

    Returns:
        Sanitized filename with extension
    """
    # Sanitize: replace invalid chars with underscore
    # Invalid chars: / \ : * ? " < > |
    sanitized = re.sub(r'[/\\:*?"<>|]', "_", title)

    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip(". ")

    # Fallback for empty titles
    if not sanitized:
        sanitized = "untitled"

    # Reserve space for duplicate suffix
    effective_max = max_length - DUPLICATE_SUFFIX_RESERVE

    # Truncate if too long
    if len(sanitized) > effective_max:
        sanitized = sanitized[:effective_max].rstrip(". ")

    # Build initial filename
    base = sanitized
    filename = f"{base}{extension}"

    # Handle duplicates with (2), (3), etc.
    counter = 2
    while filename in existing_files:
        filename = f"{base} ({counter}){extension}"
        counter += 1

    return filename
