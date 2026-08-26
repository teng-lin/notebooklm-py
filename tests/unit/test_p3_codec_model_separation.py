"""P3 codec-to-record-to-public-model compatibility characterization."""

from __future__ import annotations

import copy
import pickle
from dataclasses import fields

import pytest

from notebooklm._semantic.projectors import (
    project_artifact,
    project_collection,
    project_label,
    project_notebook,
    project_notebook_description,
    project_report_suggestion,
    project_share_status,
    project_source,
)
from notebooklm._web.codec.artifacts import (
    decode_artifact,
    decode_mind_map_artifact,
    decode_report_suggestion,
)
from notebooklm._web.codec.collections import decode_collection
from notebooklm._web.codec.labels import decode_label
from notebooklm._web.codec.notebooks import decode_notebook, decode_notebook_description
from notebooklm._web.codec.sharing import decode_share_status
from notebooklm._web.codec.sources import decode_source
from notebooklm.rpc import RPCMethod
from notebooklm.types import (
    Artifact,
    Collection,
    Label,
    Notebook,
    NotebookDescription,
    ReportSuggestion,
    SharedUser,
    ShareStatus,
    Source,
    UnknownTypeWarning,
)


def test_codec_projectors_match_every_retained_public_factory() -> None:
    notebook_row = [" thought\nNotebook ", ["s"], "nb", "📓", None, [3]]
    source_row = [["src"], "Source", [None, 7, [1_704_067_200, 0], None, 5], [None, 2]]
    artifact_row = ["art", "Artifact", 2, None, 3]
    mind_map_row = ["map", ["map", "{}", [1, "u", [1_704_067_200, 0]], None, "Map"]]
    label_row = ["Topic", [["src"]], "label", "📁"]
    collection_row = ["Research", ["nb"], "collection", "📁"]
    share_row = [[["owner@example.com", 1, [], ["Owner", None]]], [True], 1000, True]
    description_row = [[["Summary"], [[["Question", "Prompt"]]]]]
    suggestion_row = ["Briefing", "Description", None, None, "Prompt", 1]

    assert project_notebook(decode_notebook(notebook_row)) == Notebook.from_api_response(
        notebook_row
    )
    assert project_source(
        decode_source(source_row, method_id=RPCMethod.ADD_SOURCE.value)
    ) == Source.from_api_response(source_row, method_id=RPCMethod.ADD_SOURCE.value)
    assert project_artifact(decode_artifact(artifact_row)) == Artifact.from_api_response(
        artifact_row
    )
    mind_map_record = decode_mind_map_artifact(mind_map_row)
    assert mind_map_record is not None
    assert project_artifact(mind_map_record) == Artifact.from_mind_map(mind_map_row)
    assert project_label(
        decode_label(label_row, notebook_id="nb", method_id=RPCMethod.LIST_LABELS.value)
    ) == Label.from_api_response(label_row, notebook_id="nb", method_id=RPCMethod.LIST_LABELS.value)
    assert project_collection(
        decode_collection(collection_row, method_id=RPCMethod.LIST_LABELS.value)
    ) == Collection.from_api_response(collection_row, method_id=RPCMethod.LIST_LABELS.value)
    assert project_share_status(
        decode_share_status(share_row, "nb")
    ) == ShareStatus.from_api_response(share_row, "nb")
    assert project_notebook_description(
        decode_notebook_description(description_row)
    ) == NotebookDescription.from_api_response(
        {"summary": "Summary", "suggested_topics": [{"question": "Question", "prompt": "Prompt"}]}
    )
    assert project_report_suggestion(
        decode_report_suggestion(suggestion_row)
    ) == ReportSuggestion.from_api_response(
        {
            "title": "Briefing",
            "description": "Description",
            "prompt": "Prompt",
            "audience_level": 1,
        }
    )


def test_unknown_wire_enums_remain_lossless_until_public_projection() -> None:
    source = project_source(decode_source([["src"], "Source", [None, 0, None, None, 991_337]]))
    assert source._type_code == 991_337
    with pytest.warns(UnknownTypeWarning, match="991337"):
        assert source.kind.value == "unknown"

    artifact_record = decode_artifact(["art", "Artifact", 991_338, None, 991_339])
    assert artifact_record.family == "unknown"
    assert artifact_record.unrecognized_family == 991_338
    assert artifact_record.status == "unknown"
    assert artifact_record.unrecognized_status == 991_339
    artifact = project_artifact(artifact_record)
    assert artifact._artifact_type == 991_338
    assert artifact.status == 991_339
    with pytest.warns(UnknownTypeWarning, match="991338"):
        assert artifact.kind.value == "unknown"


def test_absent_source_kind_does_not_collapse_to_wire_unknown_zero() -> None:
    record = decode_source([["src"], "Source", [None, 0, None, None]])

    assert record.kind == "unknown"
    assert record.kind_present is False
    assert project_source(record)._type_code is None


def test_projectors_preserve_field_order_pickle_deepcopy_and_assignment_invariants() -> None:
    notebook = project_notebook(
        decode_notebook(
            ["Notebook", [], "nb", "📓", None, [1, False, True, None, None, [10], None, None, [5]]]
        )
    )
    assert [field.name for field in fields(notebook)] == [field.name for field in fields(Notebook)]
    assert notebook.modified_at == notebook.last_viewed_at
    for clone in (pickle.loads(pickle.dumps(notebook)), copy.deepcopy(notebook)):
        assert clone == notebook
        assert clone.modified_at == clone.last_viewed_at
        assert clone.is_owner is (clone.role is None or clone.role.value == 1)

    replacement = copy.deepcopy(notebook)
    replacement.last_viewed_at = notebook.last_viewed_at
    assert replacement.modified_at == replacement.last_viewed_at


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (Notebook, "from_api_response"),
        (Source, "from_row"),
        (Source, "from_api_response"),
        (Artifact, "from_api_response"),
        (Artifact, "from_mind_map"),
        (Label, "from_api_response"),
        (Collection, "from_api_response"),
        (SharedUser, "from_api_response"),
        (ShareStatus, "from_api_response"),
        (NotebookDescription, "from_api_response"),
        (ReportSuggestion, "from_api_response"),
    ],
)
def test_retained_public_factories_remain_importable(owner: type[object], name: str) -> None:
    assert callable(getattr(owner, name))
