"""Behavioural tests for the public shim modules.

The static surface contracts — the documented public-import manifest, the
re-export identity pins, the frozen ``__all__`` ordering, the removed-name
guards, the facade-delegates-via-reflection checks, and the auth first-party
seam manifest — moved to ``tests/_guardrails/test_public_surface_manifest.py``
as part of the test-guardrail consolidation. What remains here are the
functions that *exercise runtime behaviour* through the public surface: the
``select_cited_sources`` / ``ResearchAPI`` back-compat delegations, the
``UnknownTypeWarning`` filter behaviour, and the ``NotebookLMClient.rpc_call``
kwarg-forwarding path.
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.repo_lint


# ---------------------------------------------------------------------------
# notebooklm.research back-compat behaviour
# ---------------------------------------------------------------------------


def test_research_select_cited_sources_returns_public_dataclass():
    """select_cited_sources returns the public CitedSourceSelection dataclass."""
    from notebooklm.research import select_cited_sources
    from notebooklm.types import CitedSourceSelection

    result = select_cited_sources([], "")
    assert isinstance(result, CitedSourceSelection)
    assert result.used_fallback is True


def test_research_api_backward_compat_classmethod_delegates():
    """notebooklm._research.ResearchAPI.select_cited_sources still works."""
    from notebooklm._research import ResearchAPI
    from notebooklm.types import CitedSourceSelection

    result = ResearchAPI.select_cited_sources([], "")
    assert isinstance(result, CitedSourceSelection)


def test_research_api_extract_report_urls_backward_compat_classmethod_delegates(
    monkeypatch: pytest.MonkeyPatch,
):
    """notebooklm._research.ResearchAPI.extract_report_urls still works."""
    import notebooklm.research as research_module
    from notebooklm._research import ResearchAPI

    report = "See [Example](https://Example.com/path/)."
    sentinel = {"delegated"}
    calls: list[str] = []

    def fake_extract_report_urls(value: str) -> set[str]:
        calls.append(value)
        return sentinel

    monkeypatch.setattr(research_module, "extract_report_urls", fake_extract_report_urls)

    assert ResearchAPI.extract_report_urls(report) is sentinel
    assert calls == [report]


def test_research_api_reexports_cited_source_selection_for_back_compat():
    """notebooklm._research.CitedSourceSelection continues to resolve."""
    from notebooklm._research import CitedSourceSelection as Legacy
    from notebooklm.types import CitedSourceSelection

    assert Legacy is CitedSourceSelection


# ---------------------------------------------------------------------------
# UnknownTypeWarning filter / parser dedup behaviour
# ---------------------------------------------------------------------------


def test_types_private_state_seams_are_live_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning-dedup sets and compat map must remain shared between canonical
    owners in ``_types/`` and their ``notebooklm.types`` re-exports."""
    import notebooklm.types as public_types
    from notebooklm._types import artifacts as _artifact_types_seam
    from notebooklm._types import sources as _source_types_seam
    from notebooklm.types import (
        _SOURCE_TYPE_COMPAT_MAP,
        Artifact,
        ArtifactType,
        Source,
        SourceType,
        UnknownTypeWarning,
    )

    assert _SOURCE_TYPE_COMPAT_MAP is public_types._SOURCE_TYPE_COMPAT_MAP
    source_warnings: set[int] = set()
    artifact_warnings: set[tuple[int | None, int | None]] = set()
    # ADR-0007 seam-aliased object-target form: patch the canonical owners in
    # ``_types/{sources,artifacts}`` directly. ``notebooklm.types._warned_*``
    # are re-exports of those canonical objects, so the test must target the
    # canonical home — patching the public facade would rebind only the
    # facade alias and not the module-global the parser reads.
    monkeypatch.setattr(_source_types_seam, "_warned_source_types", source_warnings)
    monkeypatch.setattr(_artifact_types_seam, "_warned_artifact_types", artifact_warnings)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnknownTypeWarning)
        assert Source(id="source", _type_code=7654321).kind is SourceType.UNKNOWN
        assert (
            Artifact(id="artifact", title="Artifact", _artifact_type=7654322, status=3).kind
            is ArtifactType.UNKNOWN
        )

    assert 7654321 in source_warnings
    assert (7654322, None) in artifact_warnings


def test_facade_unknown_type_warning_filter_suppresses_parser_warnings() -> None:
    """Filters using notebooklm.types.UnknownTypeWarning still catch parser warnings."""
    import notebooklm.types as public_types

    public_types._warned_source_types.clear()
    public_types._warned_artifact_types.clear()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=public_types.UnknownTypeWarning)

        assert (
            public_types.Source(id="source", _type_code=8765432).kind
            is public_types.SourceType.UNKNOWN
        )
        assert (
            public_types.Artifact(
                id="artifact",
                title="Artifact",
                _artifact_type=8765433,
                status=3,
            ).kind
            is public_types.ArtifactType.UNKNOWN
        )

    assert caught == []
    assert 8765432 in public_types._warned_source_types
    assert (8765433, None) in public_types._warned_artifact_types


# ---------------------------------------------------------------------------
# auth cookie-validation behaviour
# ---------------------------------------------------------------------------


def test_auth_validation_uses_cookie_policy_rebindings_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation rebindings must target the canonical home in ``_auth.cookie_policy``.

    Wave 4 T2.2 of the session-decoupling plan removed the auth.py
    write-through that previously copy-forwarded facade-level rebinds into
    ``_cookie_policy``. Tests that want to rebind policy names patch the
    canonical module directly.
    """
    from notebooklm import auth
    from notebooklm._auth import cookie_policy

    monkeypatch.setattr(cookie_policy, "MINIMUM_REQUIRED_COOKIES", {"SID"})
    monkeypatch.setattr(cookie_policy, "_has_valid_secondary_binding", lambda names: True)

    # ``auth._validate_required_cookies`` is the same object as
    # ``cookie_policy._validate_required_cookies`` (see the identity gate in
    # ``tests/_guardrails/test_public_surface_manifest.py``), so calling
    # either reaches the canonical implementation which observes the rebind.
    auth._validate_required_cookies({"SID"})


def test_auth_validation_extraction_hint_lives_on_cookie_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validator's error message uses ``_cookie_policy._EXTRACTION_HINT``."""
    from notebooklm import auth
    from notebooklm._auth import cookie_policy

    monkeypatch.setattr(cookie_policy, "MINIMUM_REQUIRED_COOKIES", {"SID", "SIDTS"})
    monkeypatch.setattr(cookie_policy, "_EXTRACTION_HINT", "custom extraction hint")

    with pytest.raises(ValueError, match="custom extraction hint"):
        auth._validate_required_cookies({"SID"})


# ---------------------------------------------------------------------------
# NotebookLMClient.rpc_call kwarg forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_rpc_call_forwards_supported_kwargs() -> None:
    """NotebookLMClient.rpc_call forwards its supported kwargs to the executor.

    After the v0.6.0 cut, the public wrapper exposes only the supported
    surface (``method``, ``params``, ``allow_null``, and the keyword-only
    ``disable_internal_retries``); the previously-deprecated
    ``source_path`` / ``_is_retry`` / ``operation_variant`` kwargs were
    removed and are no longer forwarded by this layer.
    """
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.rpc import RPCMethod
    from tests._fixtures.fake_core import make_fake_core

    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    # ADR-0007 constructor injection: substitute the whole executor
    # collaborator with the seam fixture's fake instead of mutating
    # ``client._rpc_executor.rpc_call`` after the fact. The public
    # ``rpc_call`` wrapper reads ``self._rpc_executor``, so swapping the
    # executor exercises the same forwarding path.
    fake = make_fake_core(rpc_call=AsyncMock(return_value={"ok": True}))
    client._rpc_executor = fake.rpc_executor

    result = await client.rpc_call(
        RPCMethod.CREATE_NOTEBOOK,
        ["My Notebook"],
        allow_null=True,
        disable_internal_retries=True,
    )

    assert result == {"ok": True}
    fake.rpc_executor.rpc_call.assert_awaited_once_with(
        method=RPCMethod.CREATE_NOTEBOOK,
        params=["My Notebook"],
        allow_null=True,
        disable_internal_retries=True,
    )


@pytest.mark.asyncio
async def test_client_rpc_call_forwards_default_arguments() -> None:
    """The default-shape call forwards minimal kwargs and inherits executor defaults."""
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.rpc import RPCMethod
    from tests._fixtures.fake_core import make_fake_core

    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    # No async context is needed: ADR-0007 constructor injection swaps the
    # whole executor collaborator for the seam fixture's fake before any
    # real transport initialization can be required.
    fake = make_fake_core(rpc_call=AsyncMock(return_value=[]))
    client._rpc_executor = fake.rpc_executor

    result = await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    assert result == []
    # The wrapper forwards only the kwargs it owns; the rest of
    # RpcExecutor.rpc_call's signature (source_path, _is_retry,
    # operation_variant) keeps its module-level defaults.
    fake.rpc_executor.rpc_call.assert_awaited_once_with(
        method=RPCMethod.LIST_NOTEBOOKS,
        params=[],
        allow_null=False,
        disable_internal_retries=False,
    )
