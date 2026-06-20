"""Single-argument golden mappers for ``test_rpc_golden_payloads``.

Each function here takes exactly one argument — a fixture's
``response.expected_decoded`` payload — and returns the feature-level
domain object(s) that the production code builds from that same decoded
payload. The golden test (``test_mapper_output_shape_when_documented``)
imports these via the fixture's ``"mapper": "tests.unit._golden_mappers:<fn>"``
field, runs the function on ``expected_decoded``, projects the result into its
public shape (``to_public_dict()`` when present, else the transport-neutral
:func:`notebooklm._app.serialize.to_jsonable`), and pins it against the
fixture's recorded ``mapper_expected`` shape.

Why a dedicated test-side module rather than wiring the production factories
directly?

* The golden harness calls the mapper with a single positional argument, but
  several production factories take extra arguments
  (``Source.from_api_response(data, *, method_id=...)``,
  ``ShareStatus.from_api_response(data, notebook_id)``,
  ``Label.from_api_response(data, *, notebook_id=..., method_id=...)``), and
  some methods return a *list* of rows that the feature layer extracts before
  mapping (``LIST_NOTEBOOKS`` / ``LIST_ARTIFACTS`` / ``LIST_LABELS`` /
  ``GET_SUGGESTED_REPORTS``).
* These thin adapters mirror the **exact** extraction-then-factory path the
  feature layer runs (see the cross-references in each docstring), so the
  golden seam exercises the real decode->dataclass boundary without leaking
  test-only logic into ``src/``.

Methods whose feature path has no clean single-payload mapper (inline
``safe_index`` extraction, fire-and-forget ``None`` returns, or a
``SourceFulltext`` built field-by-field) are deliberately left without a
mapper entry and remain honestly skipped (see ``_MAPPER_COVERED_METHODS`` in
the test module for the exemption rationale).
"""

from __future__ import annotations

from typing import Any

from notebooklm._row_adapters.artifacts import ReportSuggestionRow, unwrap_artifact_rows
from notebooklm._row_adapters.notebooks import PromptSuggestionRow, unwrap_prompt_suggestions
from notebooklm._types.artifacts import Artifact, ReportSuggestion
from notebooklm._types.labels import Label
from notebooklm._types.notebooks import Notebook, PromptSuggestion
from notebooklm._types.sharing import ShareStatus
from notebooklm._types.sources import Source
from notebooklm.rpc.types import RPCMethod

# Fixed notebook id used by the share-status / label mappers below. The
# ``GET_SHARE_STATUS`` / ``LIST_LABELS`` decoded payloads carry no notebook
# reference (the id is supplied by the caller at the feature layer), so the
# golden mapper pins a synthetic, scrubbed id that matches the one used in the
# corresponding fixtures' request params.
_NOTEBOOK_ID = "SCRUBBED_NB_001"


def list_notebooks(decoded: Any) -> list[Notebook]:
    """``LIST_NOTEBOOKS`` -> one :class:`Notebook` per row.

    Mirrors ``NotebooksAPI.list`` (``_notebooks.py``): the decoded payload is
    the ``[[row, ...]]`` wrapped envelope, and each inner row is handed to
    :meth:`Notebook.from_api_response`.
    """
    return [Notebook.from_api_response(row) for row in decoded[0]]


def get_notebook(decoded: Any) -> Notebook:
    """``GET_NOTEBOOK`` -> a single :class:`Notebook`.

    Mirrors ``NotebooksAPI.get`` (``_notebooks.py``): ``decoded[0]`` is the
    notebook-info row passed to :meth:`Notebook.from_api_response`.
    """
    return Notebook.from_api_response(decoded[0])


def add_source(decoded: Any) -> Source:
    """``ADD_SOURCE`` -> the created :class:`Source`.

    Mirrors ``_source/add.py``: the decoded payload is handed straight to
    :meth:`Source.from_api_response` tagged with the ``ADD_SOURCE`` method id.
    """
    return Source.from_api_response(decoded, method_id=RPCMethod.ADD_SOURCE.value)


def list_artifacts(decoded: Any) -> list[Artifact]:
    """``LIST_ARTIFACTS`` -> one :class:`Artifact` per studio row.

    Mirrors ``ArtifactsAPI`` studio-row filtering
    (``_artifact/listing.py::_filter_studio_artifacts``): the decoded payload
    is routed through the production ``unwrap_artifact_rows`` wrap-probe — which
    accepts both the wrapped ``[[row, ...]]`` envelope and an already-flat list —
    and each non-empty list row is built via :meth:`Artifact.from_api_response`.
    (Mind-map rows come from a separate ``GET_NOTES_AND_MIND_MAPS`` fetch and are
    not part of this payload.)
    """
    rows = unwrap_artifact_rows(
        decoded,
        method_id=RPCMethod.LIST_ARTIFACTS.value,
        source="golden.list_artifacts",
    )
    return [
        Artifact.from_api_response(row) for row in rows if isinstance(row, list) and len(row) > 0
    ]


def list_labels(decoded: Any) -> list[Label]:
    """``LIST_LABELS`` -> one :class:`Label` per 4-tuple.

    Mirrors ``LabelsAPI`` parsing (``_labels.py``): the decoded payload is the
    ``[[tuple, ...]]`` envelope and each tuple is parsed via
    :meth:`Label.from_api_response` with the notebook id and method id the
    feature layer threads through.
    """
    return [
        Label.from_api_response(
            tuple_,
            notebook_id=_NOTEBOOK_ID,
            method_id=RPCMethod.LIST_LABELS.value,
        )
        for tuple_ in decoded[0]
    ]


def get_share_status(decoded: Any) -> ShareStatus:
    """``GET_SHARE_STATUS`` -> a :class:`ShareStatus`.

    Mirrors ``SharingAPI`` (``_sharing.py``): the decoded payload and the
    notebook id are handed to :meth:`ShareStatus.from_api_response`.
    """
    return ShareStatus.from_api_response(decoded, _NOTEBOOK_ID)


def get_suggested_reports(decoded: Any) -> list[ReportSuggestion]:
    """``GET_SUGGESTED_REPORTS`` -> one :class:`ReportSuggestion` per row.

    Mirrors ``ArtifactsAPI.suggest_reports`` (``_artifacts.py``): the decoded
    payload is routed through the production ``unwrap_artifact_rows`` wrap-probe
    (accepting both the wrapped ``[[row, ...]]`` envelope and a flat list), then
    each well-formed row is wrapped in a :class:`ReportSuggestionRow` (the
    position adapter) before constructing the public :class:`ReportSuggestion`.
    """
    rows = unwrap_artifact_rows(
        decoded,
        method_id=RPCMethod.GET_SUGGESTED_REPORTS.value,
        source="golden.get_suggested_reports",
    )
    return [
        ReportSuggestion(
            title=row.title,
            description=row.description,
            prompt=row.prompt,
            audience_level=row.audience_level,
        )
        for row in map(ReportSuggestionRow, rows)
        if row.is_well_formed
    ]


def suggest_prompts(decoded: Any) -> list[PromptSuggestion]:
    """``SUGGEST_PROMPTS`` -> one :class:`PromptSuggestion` per row.

    Mirrors ``NotebooksAPI.suggest_prompts`` (``_notebooks.py``): the decoded payload
    is the single-element ``[[ [title, prompt], ... ]]`` envelope routed through
    the production ``unwrap_prompt_suggestions`` (``result[0]``), then each
    well-formed row is wrapped in a :class:`PromptSuggestionRow` (the position
    adapter) before constructing the public :class:`PromptSuggestion`.
    """
    rows = unwrap_prompt_suggestions(decoded, source="golden.suggest_prompts")
    return [
        PromptSuggestion(title=row.title, prompt=row.prompt)
        for row in map(PromptSuggestionRow, rows)
        if row.is_well_formed
    ]
