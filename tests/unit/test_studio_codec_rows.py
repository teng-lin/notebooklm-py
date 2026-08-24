"""Golden payloads and decoders for the P9.3 Studio codec rows.

Each ``encode_*`` returns the exact params, notebook route and option flags the
P5 handler passed to ``_rpc_call``; each ``decode_*`` wraps the existing
projection.  These are the moved-encoder goldens the P9.3 acceptance criteria
require.
"""

from __future__ import annotations

import pytest

from notebooklm._artifact.payloads import build_retry_artifact_params, build_revise_slide_params
from notebooklm._backend import BackendContractError, BackendError, BackendErrorReason
from notebooklm._binding import CodecPayload
from notebooklm._operations import Operation
from notebooklm._records import (
    ArtifactDeleteInput,
    ArtifactDeleteResult,
    ArtifactDownloadInput,
    ArtifactPollInput,
    ArtifactRetryInput,
    ArtifactReviseSlideInput,
    DriveExportInput,
    DriveExportResult,
)
from notebooklm._web.codec import artifacts as artifacts_codec
from notebooklm._web.codec import studio_documents as documents_codec
from notebooklm.exceptions import DecodingError
from notebooklm.rpc import ARTIFACT_STATUS_SUGGESTED_WIRE_NAME, RPCMethod

_CATALOG_PARAMS = [[2], "nb", f'NOT artifact.status = "{ARTIFACT_STATUS_SUGGESTED_WIRE_NAME}"']


def test_studio_catalog_params_golden() -> None:
    assert artifacts_codec.encode_studio_catalog_params("nb") == _CATALOG_PARAMS


def test_delete_payload_golden() -> None:
    payload = artifacts_codec.encode_artifact_delete(ArtifactDeleteInput("nb", "artifact-id"))
    assert payload == CodecPayload(
        params=[[2], "artifact-id"], source_path="/notebook/nb", allow_null=True
    )
    assert payload.raise_on_null_status is False
    assert payload.attempt_timeout is None
    assert (
        artifacts_codec.decode_artifact_delete(ArtifactDeleteInput("nb", "artifact-id"), None)
        == ArtifactDeleteResult()
    )


@pytest.mark.parametrize(("destination", "code"), [("docs", 1), ("sheets", 2)])
def test_export_payload_golden(destination: str, code: int) -> None:
    value = DriveExportInput("nb", "artifact-id", "body", "Title", destination)
    payload = artifacts_codec.encode_artifact_export(value)
    assert payload.params == [None, "artifact-id", "body", "Title", code]
    assert payload.source_path == "/notebook/nb"
    assert payload.allow_null is True
    assert payload.raise_on_null_status is False
    assert artifacts_codec.decode_artifact_export(value, {"ok": 1}) == DriveExportResult({"ok": 1})


def test_export_rejects_unknown_destinations_as_a_contract_error() -> None:
    with pytest.raises(BackendContractError, match="unrecognized Drive export destination") as e:
        artifacts_codec.encode_artifact_export(DriveExportInput("nb", destination="slides"))
    assert e.value.operation is Operation.ARTIFACT_EXPORT


def test_wait_payload_and_decoder_golden() -> None:
    value = ArtifactPollInput("nb", "task-id")
    payload = artifacts_codec.encode_artifact_wait(value)
    assert payload == CodecPayload(
        params=_CATALOG_PARAMS, source_path="/notebook/nb", allow_null=True
    )
    observed = artifacts_codec.decode_artifact_wait(value, [["task-id", "Deck", 8, None, 3]])
    assert observed.status.task_id == "task-id"
    assert observed.status.status == "in_progress"
    assert artifacts_codec.decode_artifact_wait(value, None).status.status == "not_found"
    assert artifacts_codec.decode_artifact_wait(value, []).status.status == "not_found"


def test_studio_rows_decoder_fails_loud_on_unrecognized_shapes() -> None:
    with pytest.raises(DecodingError, match="Unrecognized LIST_ARTIFACTS payload shape") as e:
        artifacts_codec.decode_studio_rows({"rows": []}, source="test")
    assert e.value.method_id == RPCMethod.LIST_ARTIFACTS.value


@pytest.mark.parametrize(
    ("action", "artifact_id", "params"),
    [
        ("catalog", None, _CATALOG_PARAMS),
        ("mind_maps", None, ["nb"]),
        ("interactive_html", "artifact-id", ["artifact-id"]),
        ("mind_map_tree", "artifact-id", ["artifact-id"]),
    ],
)
def test_download_payload_golden(
    action: str, artifact_id: str | None, params: list[object]
) -> None:
    payload = artifacts_codec.encode_artifact_download(
        ArtifactDownloadInput("nb", action, artifact_id)
    )
    assert payload == CodecPayload(params=params, source_path="/notebook/nb", allow_null=True)


def test_download_payload_rejects_the_inputs_the_handler_rejected() -> None:
    with pytest.raises(BackendContractError, match="requires artifact_id") as missing:
        artifacts_codec.encode_artifact_download(ArtifactDownloadInput("nb", "mind_map_tree"))
    assert missing.value.operation is Operation.ARTIFACT_DOWNLOAD
    with pytest.raises(BackendContractError, match="unrecognized artifact.download action"):
        artifacts_codec.encode_artifact_download(ArtifactDownloadInput("nb", "wire"))
    with pytest.raises(BackendContractError, match="unrecognized artifact.download action"):
        artifacts_codec.decode_artifact_download(ArtifactDownloadInput("nb", "wire"), [])


def test_download_decoder_branches_on_the_action() -> None:
    assert (
        artifacts_codec.decode_artifact_download(
            ArtifactDownloadInput("nb", "catalog"), []
        ).representations
        == ()
    )
    assert (
        artifacts_codec.decode_artifact_download(
            ArtifactDownloadInput("nb", "mind_maps"), []
        ).mind_maps
        == ()
    )
    html = artifacts_codec.decode_artifact_download(
        ArtifactDownloadInput("nb", "interactive_html", "a"), [[None] * 9 + [["<html>"]]]
    )
    assert html.content == "<html>"
    tree = artifacts_codec.decode_artifact_download(
        ArtifactDownloadInput("nb", "mind_map_tree", "a"),
        [[None] * 9 + [[None, None, None, "{}"]]],
    )
    assert tree.content == "{}"


def test_revise_slide_payload_and_decoder_golden() -> None:
    value = ArtifactReviseSlideInput("nb", "deck", 2, "Improve")
    payload = documents_codec.encode_artifact_revise_slide(value)
    assert payload.params == build_revise_slide_params("deck", 2, "Improve")
    assert payload.source_path == "/notebook/nb"
    assert payload.allow_null is True
    assert payload.raise_on_null_status is True
    revised = documents_codec.decode_artifact_revise_slide(value, [["deck", None, None, None, 1]])
    assert revised.status.task_id == "deck"


def test_retry_payload_and_decoder_golden() -> None:
    value = ArtifactRetryInput("nb", "retry-id")
    payload = documents_codec.encode_artifact_retry(value)
    assert payload.params == build_retry_artifact_params("retry-id")
    assert payload.source_path == "/notebook/nb"
    assert payload.allow_null is True
    assert payload.raise_on_null_status is True
    retried = documents_codec.decode_artifact_retry(value, [["retry-id", None, None, None, 1]])
    assert retried.status.task_id == "retry-id"


@pytest.mark.parametrize(
    ("decode", "value", "artifact_type", "operation", "method"),
    [
        (
            documents_codec.decode_artifact_revise_slide,
            ArtifactReviseSlideInput("nb", "deck", 2, "Improve"),
            "slide revision",
            Operation.ARTIFACT_REVISE_SLIDE,
            RPCMethod.REVISE_SLIDE,
        ),
        (
            documents_codec.decode_artifact_retry,
            ArtifactRetryInput("nb", "retry-id"),
            "retry",
            Operation.ARTIFACT_RETRY,
            RPCMethod.RETRY_ARTIFACT,
        ),
    ],
)
def test_null_kickoff_decodes_to_the_closed_unavailable_error(
    decode: object, value: object, artifact_type: str, operation: Operation, method: RPCMethod
) -> None:
    with pytest.raises(BackendError) as caught:
        decode(value, None)  # type: ignore[operator]
    error = caught.value
    assert error.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    assert error.operation is operation
    assert error.message == f"{artifact_type.capitalize()} generation is unavailable"
    assert dict(error.diagnostics or {}) == {
        "artifact_type": artifact_type,
        "method_id": method.value,
        "raw_response": None,
    }
