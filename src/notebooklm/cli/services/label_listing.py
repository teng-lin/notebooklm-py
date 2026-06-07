"""Service for the ``label`` CLI group — resolution + the list join.

ADR-0008: this is a ``cli/services`` module, so it is boundary-clean (no Click
imports, no ``..rendering`` / ``..error_handler`` / ``..runtime`` imports, and
it never writes to stdout). It returns a :class:`~notebooklm.cli.services.listing.ListRender`
for the command layer to render and raises a typed :class:`LabelResolutionError`
the command layer maps through the ADR-0015 error contract.

Two responsibilities live here:

* :func:`resolve_label_id` — the composite ``<id|name>`` resolver. It is **not**
  a mirror of ``resolve_source_id`` (which is id/prefix-only). It tries the
  id/prefix pass with full-id passthrough **disabled**, then falls back to an
  explicit exact-**name** match over ``client.labels.list()`` (a name pass —
  ``resolve_partial_id_in_items``'s ``title_of`` is diagnostics-only and does
  not make names matchable). An ambiguous *prefix* (>1 id match) raises with the
  candidate list (code ``AMBIGUOUS_ID``) BEFORE the name fallback — without this
  it would wrongly fall through to a NOT_FOUND. An ambiguous *name* (>1 match)
  raises with the candidate ids, emojis, and source counts; it never guesses.
* The members→titles join + :func:`execute_label_list` — one ``labels.list()``
  plus one ``sources.list()`` build the ``{source_id: title}`` map, so each
  label's members carry resolved titles without an N+1 fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...types import Label, Source
from ..resolve import resolve_partial_id_in_items, validate_id
from .listing import ListRender, ListSpec, prepare_list

if TYPE_CHECKING:
    from ...client import NotebookLMClient


class LabelResolutionError(Exception):
    """Typed label-resolution error for command-layer rendering and exit policy.

    Mirrors ``SourceMutationError``: carries a human ``message``, an
    ADR-0015 ``code`` (``NOT_FOUND`` / ``AMBIGUOUS_ID`` / ``AMBIGUOUS_NAME`` /
    ``VALIDATION_ERROR``), and an optional ``extra`` payload. The command layer
    maps it through ``output_error`` so the typed ``--json`` envelope is
    preserved.
    """

    def __init__(
        self,
        message: str,
        code: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.extra = extra
        super().__init__(f"{message} (code={code})")


async def resolve_label_id(
    client: NotebookLMClient,
    notebook_id: str,
    token: str,
    *,
    json_output: bool = False,
) -> str:
    """Resolve a label ``<id|name>`` token to a full label id.

    Resolution order: id / unambiguous-prefix first (full-id passthrough
    **disabled** so a UUID-shaped *name* is not blindly accepted as an id),
    then an explicit exact-name match. An ambiguous *prefix* (>1 id match)
    raises with code ``AMBIGUOUS_ID`` BEFORE the name fallback; an ambiguous
    *name* (>1 match) raises with code ``AMBIGUOUS_NAME``. Both
    :class:`LabelResolutionError` paths list each candidate's id, emoji, and
    source count.
    """
    token = validate_id(token, "label")
    labels = await client.labels.list(notebook_id)

    # Pass 1: id / unambiguous-prefix. Full-id passthrough is disabled so a
    # canonical-UUID token that is not an actual label id falls through to the
    # name pass below instead of being accepted verbatim.
    try:
        return resolve_partial_id_in_items(
            token,
            labels,
            entity_name="label",
            list_command="label list",
            id_of=lambda label: label.id,
            title_of=lambda label: label.name,
            error_factory=_IdPassMiss,
            emit_match_status=False,
            json_output=json_output,
            allow_full_id_passthrough=False,
        )
    except _IdPassMiss:
        # ``resolve_partial_id_in_items`` raises ``_IdPassMiss`` for BOTH "no
        # match" AND "ambiguous prefix" (see ``cli/resolve.py``). Distinguish the
        # ambiguous-prefix case here so it surfaces its candidate list (mirroring
        # ``resolve_source_id``) instead of silently falling through to the name
        # pass and reporting a misleading NOT_FOUND. Exact-id matches already
        # returned from pass 1 above, so a >1 prefix-match count here is a true
        # ambiguity, not a short-but-complete id.
        token_lower = token.lower()
        prefix_matches = [label for label in labels if label.id.lower().startswith(token_lower)]
        if len(prefix_matches) > 1:
            raise LabelResolutionError(
                _ambiguous_id_message(token, prefix_matches),
                "AMBIGUOUS_ID",
                {
                    "id": token,
                    "candidates": [
                        {
                            "id": label.id,
                            "emoji": label.emoji,
                            "source_count": len(label.source_ids),
                        }
                        for label in prefix_matches
                    ],
                },
            ) from None
        # No id / prefix match — fall through to the explicit name pass.

    # Pass 2: explicit exact-name match (``title_of`` is diagnostics-only in
    # ``resolve_partial_id_in_items`` and does not make names matchable).
    name_matches = [label for label in labels if label.name == token]
    if len(name_matches) == 1:
        return name_matches[0].id
    if len(name_matches) > 1:
        raise LabelResolutionError(
            _ambiguous_name_message(token, name_matches),
            "AMBIGUOUS_NAME",
            {
                "name": token,
                "candidates": [
                    {
                        "id": label.id,
                        "emoji": label.emoji,
                        "source_count": len(label.source_ids),
                    }
                    for label in name_matches
                ],
            },
        )

    raise LabelResolutionError(
        f"No label found matching '{token}'. Run 'notebooklm label list' to see available labels.",
        "NOT_FOUND",
        {"id": token, "notebook_id": notebook_id},
    )


class _IdPassMiss(Exception):
    """Internal sentinel raised by the id/prefix pass on no match.

    Kept private so a missed id pass can fall through to the name pass; the
    public errors raised to the command layer are always
    :class:`LabelResolutionError`.
    """


def _ambiguous_name_message(name: str, matches: list[Label]) -> str:
    """Build the ambiguous-name error listing each candidate (id + emoji + count)."""
    lines = [f"Name '{name}' matches {len(matches)} labels. Use a label id instead:"]
    _append_candidate_lines(lines, matches)
    lines.append("Specify the label id to disambiguate.")
    return "\n".join(lines)


def _ambiguous_id_message(partial_id: str, matches: list[Label]) -> str:
    """Build the ambiguous-prefix error listing each candidate (id + emoji + count)."""
    lines = [f"Ambiguous label id '{partial_id}' matches {len(matches)} labels:"]
    _append_candidate_lines(lines, matches)
    lines.append("Specify more characters to disambiguate.")
    return "\n".join(lines)


def _append_candidate_lines(lines: list[str], matches: list[Label]) -> None:
    """Append the per-candidate ``id emoji (N sources)`` lines (capped at 5)."""
    for label in matches[:5]:
        emoji = f"{label.emoji} " if label.emoji else ""
        count = len(label.source_ids)
        lines.append(f"  {label.id} {emoji}({count} source{'s' if count != 1 else ''})")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")


@dataclass(frozen=True)
class LabelListPlan:
    """Prepared inputs for :func:`execute_label_list`."""

    notebook_id: str
    json_output: bool
    limit: int | None
    no_truncate: bool


def _label_serialize(label: Label, titles: dict[str, str | None]) -> dict[str, Any]:
    """Serialize a label with its members joined to resolved source titles."""
    return {
        "id": label.id,
        "name": label.name,
        "emoji": label.emoji,
        "source_ids": list(label.source_ids),
        # Include EVERY member id (title=None for any source missing from the
        # notebook list — a benign concurrent-delete race) so ``sources`` stays
        # 1:1 with ``source_ids`` and a consumer can rely on equal lengths.
        "sources": [{"id": sid, "title": titles.get(sid)} for sid in label.source_ids],
    }


async def execute_label_list(client: NotebookLMClient, plan: LabelListPlan) -> ListRender[Label]:
    """Fetch + assemble the ``label list`` render payload.

    One ``labels.list()`` + one ``sources.list()`` (the title join) — no N+1.
    """
    sources: list[Source] = await client.sources.list(plan.notebook_id)
    titles: dict[str, str | None] = {source.id: source.title for source in sources}

    async def fetch(_client: NotebookLMClient, notebook_id: str) -> list[Label]:
        return await _client.labels.list(notebook_id)

    spec = ListSpec[Label](
        title="Labels in {notebook_id}",
        items_key="labels",
        fetch=fetch,
        serialize=lambda label: _label_serialize(label, titles),
        columns=["ID", "Emoji", "Name", "Sources"],
        row=lambda label: [
            label.id,
            label.emoji or "-",
            label.name,
            str(len(label.source_ids)),
        ],
        include_index=False,
        empty_message="[yellow]No labels found[/yellow]",
    )
    return await prepare_list(
        spec,
        client,
        notebook_id=plan.notebook_id,
        limit=plan.limit,
        json_output=plan.json_output,
        no_truncate=plan.no_truncate,
    )


__all__ = [
    "LabelListPlan",
    "LabelResolutionError",
    "execute_label_list",
    "resolve_label_id",
]
