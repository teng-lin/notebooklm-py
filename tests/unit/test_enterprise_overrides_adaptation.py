"""Unit tests for NotebookLM Enterprise parameter adaptation layer."""

from __future__ import annotations

import pytest

from notebooklm.rpc import RPCMethod
from notebooklm.rpc.encoder import adapt_enterprise_params


@pytest.fixture
def enterprise_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure environment variables for Enterprise tests."""
    monkeypatch.setenv("NOTEBOOKLM_PROJECT", "test-project-123")
    monkeypatch.setenv("NOTEBOOKLM_REGION", "us-central1")


def test_adapt_enterprise_params_get_source(enterprise_env: None) -> None:
    """Verify GET_SOURCE is adapted correctly for Enterprise."""
    # Standard: [[source_id], [2], [2]]
    # Enterprise expected:
    # ["projects/{project}/locations/{region}/notebooks/{notebook}/sources/{source}", [2]]
    standard_params = [["source-abc-123"], [2], [2]]
    adapted = adapt_enterprise_params(
        RPCMethod.GET_SOURCE,
        standard_params,
        project_id="test-project-123",
        region="us-central1",
        source_path="/notebook/notebook-xyz",
    )
    assert adapted == [
        "projects/test-project-123/locations/us-central1/"
        "notebooks/notebook-xyz/sources/source-abc-123",
        [2],
    ]


def test_adapt_enterprise_params_add_source_file(enterprise_env: None) -> None:
    """Verify ADD_SOURCE_FILE is adapted correctly for Enterprise."""
    # Standard: [[[filename]], notebook_id, template_block]
    # Enterprise expected:
    # ["projects/{project}/locations/{region}/notebooks/{notebook}/sources/{source}", [[source_id]]]
    standard_params = [[["report.pdf"]], "notebook-xyz", None]
    adapted = adapt_enterprise_params(
        RPCMethod.ADD_SOURCE_FILE,
        standard_params,
        project_id="test-project-123",
        region="us-central1",
        source_path="/notebook/notebook-xyz/sources/source-uuid-v4-999",
    )
    assert adapted == [
        "projects/test-project-123/locations/us-central1/"
        "notebooks/notebook-xyz/sources/source-uuid-v4-999",
        [["source-uuid-v4-999"]],
    ]


def test_adapt_enterprise_params_get_user_settings(enterprise_env: None) -> None:
    """Verify GET_USER_SETTINGS adaptation remains correct."""
    adapted = adapt_enterprise_params(
        RPCMethod.GET_USER_SETTINGS,
        [],
        project_id="test-project-123",
        region="us-central1",
    )
    assert adapted == ["projects/test-project-123/locations/us-central1"]


def test_build_resumable_upload_start_request_enterprise(enterprise_env: None) -> None:
    """Verify build_resumable_upload_start_request constructs a fully-qualified

    notebook resource path under Enterprise.
    """
    from notebooklm._source.upload_payloads import build_resumable_upload_start_request

    request = build_resumable_upload_start_request(
        notebook_id="notebook-xyz",
        filename="research.pdf",
        file_size=4096,
        source_id="src_payload",
        content_type="application/pdf",
        base_url="https://notebooklm.cloud.google.com",
        upload_url="https://notebooklm.cloud.google.com/_/upload",
        authuser_query="authuser=1",
        authuser_header="1",
    )

    assert request.body == ""
    expected_url = (
        "https://discoveryengine.clients6.google.com/upload/v1alpha/"
        "projects/test-project-123/locations/us-central1/"
        "notebooks/notebook-xyz/sources:uploadFile"
    )
    assert request.url == expected_url
    assert request.headers["x-goog-upload-file-name"] == "cmVzZWFyY2gucGRm"
    assert request.headers["x-goog-upload-header-content-length"] == "4096"
    assert request.headers["x-goog-upload-command"] == "start"


def test_build_resumable_upload_start_request_enterprise_fully_qualified(
    enterprise_env: None,
) -> None:
    """Verify build_resumable_upload_start_request extracts the project ID

    correctly when notebook_id is already fully qualified.
    """
    from notebooklm._source.upload_payloads import build_resumable_upload_start_request

    request = build_resumable_upload_start_request(
        notebook_id=(
            "projects/766918001064/locations/global/notebooks/"
            "a70bce5e-dce0-427b-bd6e-5a1fa155f3d4"
        ),
        filename="research.pdf",
        file_size=4096,
        source_id="src_payload",
        content_type="application/pdf",
        base_url="https://notebooklm.cloud.google.com",
        upload_url="https://notebooklm.cloud.google.com/_/upload",
        authuser_query="authuser=1",
        authuser_header="1",
    )

    assert request.body == ""
    expected_url = (
        "https://discoveryengine.clients6.google.com/upload/v1alpha/"
        "projects/766918001064/locations/global/"
        "notebooks/a70bce5e-dce0-427b-bd6e-5a1fa155f3d4/sources:uploadFile"
    )
    assert request.url == expected_url
    assert request.headers["x-goog-upload-file-name"] == "cmVzZWFyY2gucGRm"
    assert request.headers["x-goog-upload-header-content-length"] == "4096"
    assert request.headers["x-goog-upload-command"] == "start"


