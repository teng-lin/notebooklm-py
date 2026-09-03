from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.e2e import conftest as e2e
from tests.e2e._artifact_helpers import completed_interactive_mind_maps

MANAGED = {
    "NOTEBOOKLM_E2E_MANAGED_COPIES": "1",
    "NOTEBOOKLM_E2E_MANAGED_MODE": "full",
    "NOTEBOOKLM_E2E_REFERENCE_PREPARED": "1",
    "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID": "reference-role",
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID": "generation-role",
    "NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID": "multi-source-role",
}


def install(monkeypatch, **overrides: str | None) -> None:
    for name, value in {**MANAGED, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_managed_full_requires_activation_mode_preparation_and_distinct_roles(monkeypatch) -> None:
    install(monkeypatch)
    assert e2e._managed_bindings() == {
        "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID": "reference-role",
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID": "generation-role",
        "NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID": "multi-source-role",
    }
    for overrides in (
        {"NOTEBOOKLM_E2E_MANAGED_COPIES": "true"},
        {"NOTEBOOKLM_E2E_MANAGED_MODE": "rpc"},
        {"NOTEBOOKLM_E2E_REFERENCE_PREPARED": None},
        {"NOTEBOOKLM_GENERATION_NOTEBOOK_ID": None},
        {"NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID": "generation-role"},
    ):
        install(monkeypatch, **overrides)
        with pytest.raises(ValueError):
            e2e._managed_bindings()


@pytest.mark.asyncio
async def test_managed_role_fixtures_do_not_touch_cache_create_cleanup_or_client(
    monkeypatch,
) -> None:
    install(monkeypatch)

    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError(f"managed fixture touched client.{name}")

    generation = e2e.generation_notebook_id.__wrapped__(ExplodingClient())
    assert await anext(generation) == "generation-role"
    with pytest.raises(StopAsyncIteration):
        await anext(generation)

    multi_source = e2e.multi_source_notebook_id.__wrapped__(ExplodingClient())
    assert await anext(multi_source) == "multi-source-role"
    with pytest.raises(StopAsyncIteration):
        await anext(multi_source)


def test_read_only_fixture_uses_managed_reference(monkeypatch) -> None:
    install(monkeypatch)
    assert e2e.read_only_notebook_id.__wrapped__() == "reference-role"


def test_managed_readonly_mode_requires_only_the_prepared_reference(monkeypatch) -> None:
    install(
        monkeypatch,
        NOTEBOOKLM_E2E_MANAGED_MODE="readonly",
        NOTEBOOKLM_GENERATION_NOTEBOOK_ID=None,
        NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID=None,
    )
    assert e2e._managed_bindings() == {"NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID": "reference-role"}
    assert e2e.read_only_notebook_id.__wrapped__() == "reference-role"


def test_managed_mind_map_download_selects_only_completed_interactive_artifacts() -> None:
    processing = SimpleNamespace(is_interactive_mind_map=True, is_completed=False)
    completed = SimpleNamespace(is_interactive_mind_map=True, is_completed=True)
    completed_other_kind = SimpleNamespace(is_interactive_mind_map=False, is_completed=True)

    assert completed_interactive_mind_maps([processing, completed, completed_other_kind]) == [
        completed
    ]


def test_unmanaged_configuration_preserves_legacy_path(monkeypatch) -> None:
    for name in MANAGED:
        monkeypatch.delenv(name, raising=False)
    assert e2e._managed_bindings() is None


def test_managed_controls_without_activation_fail_closed(monkeypatch) -> None:
    for name in MANAGED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOTEBOOKLM_E2E_MANAGED_MODE", "full")
    with pytest.raises(ValueError, match="without the activation"):
        e2e._managed_bindings()

    monkeypatch.delenv("NOTEBOOKLM_E2E_MANAGED_MODE")
    monkeypatch.setenv("NOTEBOOKLM_E2E_REFERENCE_PREPARED", "1")
    with pytest.raises(ValueError, match="without the activation"):
        e2e._managed_bindings()


def test_unconfigure_resets_first_use_cleanup_state() -> None:
    e2e._generation_cleanup_done = True
    e2e._multi_source_cleanup_done = True
    e2e.pytest_unconfigure(None)
    assert e2e._generation_cleanup_done is False
    assert e2e._multi_source_cleanup_done is False
