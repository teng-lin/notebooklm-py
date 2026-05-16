"""Tests for NotebookLM runtime endpoint configuration."""

import pytest

from notebooklm._core import ClientCore
from notebooklm._env import get_base_host, get_base_url
from notebooklm._sources import SourcesAPI
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod, get_batchexecute_url, get_query_url, get_upload_url
from notebooklm.types import ShareStatus


def test_default_base_url_is_personal(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    assert get_base_url() == "https://notebooklm.google.com"
    assert get_base_host() == "notebooklm.google.com"


def test_enterprise_base_url_via_env(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com/")

    assert get_base_url() == "https://notebooklm.cloud.google.com"
    assert get_base_host() == "notebooklm.cloud.google.com"


def test_base_url_normalizes_mixed_case_and_whitespace(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "  https://NotebookLM.Cloud.Google.com/  ")

    assert get_base_url() == "https://notebooklm.cloud.google.com"


def test_empty_base_url_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "")

    assert get_base_url() == "https://notebooklm.google.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://notebooklm.google.com",
        "https://evil.example.com",
        "https://notebooklm.google.com:443",
        "https://user:notsecret@notebooklm.google.com",
        "https://notebooklm.google.com/path",
        "https://notebooklm.google.com?x=1",
        "https://notebooklm.google.com/#fragment",
    ],
)
def test_base_url_validation_rejects_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", value)

    with pytest.raises(ValueError, match="NOTEBOOKLM_BASE_URL"):
        get_base_url()


def test_rpc_endpoint_helpers_are_lazy(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

    assert (
        get_batchexecute_url()
        == "https://notebooklm.cloud.google.com/_/LabsTailwindUi/data/batchexecute"
    )
    assert get_query_url().startswith("https://notebooklm.cloud.google.com/_/")
    assert get_upload_url() == "https://notebooklm.cloud.google.com/upload/_/"


def test_core_build_url_uses_enterprise_base_url(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")
    core = ClientCore(AuthTokens(cookies={}, csrf_token="csrf", session_id="sid"))

    # ``_build_url`` consumes an ``_AuthSnapshot`` so callers
    # outside ``_perform_authed_post`` must build one inline.
    from notebooklm._core import _AuthSnapshot

    snapshot = _AuthSnapshot(
        csrf_token=core.auth.csrf_token,
        session_id=core.auth.session_id,
        authuser=core.auth.authuser,
        account_email=core.auth.account_email,
    )
    url = core._build_url(RPCMethod.LIST_NOTEBOOKS, snapshot)

    assert url.startswith("https://notebooklm.cloud.google.com/_/LabsTailwindUi/data/")


@pytest.mark.asyncio
async def test_upload_start_uses_enterprise_url_and_headers(monkeypatch, httpx_mock):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")
    auth = AuthTokens(cookies={"SID": "test"}, csrf_token="csrf", session_id="sid")
    upload_url = "https://notebooklm.cloud.google.com/upload/_/?upload_id=test"
    httpx_mock.add_response(
        method="POST",
        url="https://notebooklm.cloud.google.com/upload/_/?authuser=0",
        headers={"x-goog-upload-url": upload_url},
    )

    core = ClientCore(auth)
    await core.open()
    try:
        result = await SourcesAPI(core)._start_resumable_upload(
            "nb_123",
            "file.txt",
            12,
            "src_123",
        )
    finally:
        await core.close()

    request = httpx_mock.get_request()
    assert result == upload_url
    assert request is not None
    assert str(request.url) == "https://notebooklm.cloud.google.com/upload/_/?authuser=0"
    assert request.headers["origin"] == "https://notebooklm.cloud.google.com"
    assert request.headers["referer"] == "https://notebooklm.cloud.google.com/"


@pytest.mark.asyncio
async def test_client_refresh_auth_uses_enterprise_base_url(monkeypatch, httpx_mock, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")
    httpx_mock.add_response(
        url="https://notebooklm.cloud.google.com/",
        text='{"SNlM0e":"fresh_csrf","FdrFJe":"fresh_sid"}',
    )
    auth = AuthTokens(cookies={"SID": "test"}, csrf_token="old", session_id="old_sid")

    async with NotebookLMClient(auth, storage_path=tmp_path / "storage.json") as client:
        refreshed = await client.refresh_auth()

    request = httpx_mock.get_request()
    assert request is not None
    assert str(request.url) == "https://notebooklm.cloud.google.com/"
    assert refreshed.csrf_token == "fresh_csrf"
    assert refreshed.session_id == "fresh_sid"


def test_share_status_uses_enterprise_base_url(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

    status = ShareStatus.from_api_response([[["owner@example.com"]], [True], 1000], "nb_123")

    assert status.share_url == "https://notebooklm.cloud.google.com/notebook/nb_123"
