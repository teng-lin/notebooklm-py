"""Tests for the public shim modules introduced by the private-module boundary plan.

Each section is owned by a different PR; please keep the markers below intact when
appending so concurrent PRs can merge cleanly.

Plan: .sisyphus/plans/private-module-boundary.md
"""

from __future__ import annotations

import importlib
import logging
import warnings
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# PR-T2 section: documented public import manifest
#
# This is the public import surface documented by PR-T1. Keep this manifest
# explicit: if docs add a new supported import path, add it here in the same PR;
# if docs intentionally remove one, remove it here with the docs change.
# ---------------------------------------------------------------------------


_DOCUMENTED_PUBLIC_IMPORTS = {
    "notebooklm": [
        "ArtifactType",
        "AudioFormat",
        "AudioLength",
        "AuthTokens",
        "ChatGoal",
        "ChatResponseLength",
        "ConnectionLimits",
        "correlation_id",
        "ExportType",
        "NonIdempotentRetryError",
        "NotebookLMClient",
        "QuizDifficulty",
        "QuizQuantity",
        "ReportFormat",
        "RPCError",
        "SharePermission",
        "ShareViewLevel",
        "SourceType",
        "VideoFormat",
        "VideoStyle",
    ],
    "notebooklm.auth": [
        "AuthTokens",
        "convert_rookiepy_cookies_to_storage_state",
        "OPTIONAL_COOKIE_DOMAINS",
        "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
        "REQUIRED_COOKIE_DOMAINS",
    ],
    "notebooklm.config": [
        "DEFAULT_BASE_URL",
        "get_base_url",
    ],
    "notebooklm.log": [
        "install_redaction",
    ],
    "notebooklm.research": [
        "extract_report_urls",
        "normalize_url",
        "select_cited_sources",
    ],
    "notebooklm.rpc": [
        "RPCMethod",
    ],
    "notebooklm.types": [
        "ConnectionLimits",
    ],
    "notebooklm.urls": [
        "is_google_auth_redirect",
        "is_youtube_url",
    ],
}


@pytest.mark.parametrize(
    ("module_name", "public_name"),
    [
        pytest.param(module_name, public_name, id=f"{module_name}:{public_name}")
        for module_name, public_names in _DOCUMENTED_PUBLIC_IMPORTS.items()
        for public_name in public_names
    ],
)
def test_documented_public_import_manifest_resolves(
    module_name: str,
    public_name: str,
) -> None:
    """Every documented public import from PR-T1 must remain importable."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        module = __import__(module_name, fromlist=[public_name])

    sentinel = object()
    assert getattr(module, public_name, sentinel) is not sentinel


def test_public_import_manifest_has_no_duplicates() -> None:
    """The manifest should stay reviewable and deterministic."""
    for module_name, public_names in _DOCUMENTED_PUBLIC_IMPORTS.items():
        assert public_names == sorted(public_names, key=str.lower), (
            f"{module_name} manifest entries must be sorted case-insensitively"
        )
        assert len(public_names) == len(set(public_names)), (
            f"{module_name} manifest contains duplicate entries"
        )


def test_public_facade_imports_are_identity_reexports() -> None:
    """Compatibility facades must keep returning the canonical public objects."""
    import notebooklm
    import notebooklm.auth as public_auth
    import notebooklm.rpc as public_rpc
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    assert notebooklm.AuthTokens is public_auth.AuthTokens
    assert notebooklm.ConnectionLimits is public_types.ConnectionLimits
    assert public_rpc.RPCMethod is rpc_types.RPCMethod


# ---------------------------------------------------------------------------
# PR-A section: notebooklm.research public surface
# ---------------------------------------------------------------------------


def test_research_module_exposes_documented_helpers():
    """notebooklm.research re-exports the three free helpers used by the CLI."""
    from notebooklm.research import (
        extract_report_urls,
        normalize_url,
        select_cited_sources,
    )

    assert callable(extract_report_urls)
    assert callable(normalize_url)
    assert callable(select_cited_sources)


def test_cited_source_selection_is_on_public_surface():
    """CitedSourceSelection lives in notebooklm.types and on the top-level package."""
    from notebooklm import CitedSourceSelection as TopLevel
    from notebooklm.types import CitedSourceSelection

    assert TopLevel is CitedSourceSelection


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


def test_research_api_reexports_cited_source_selection_for_back_compat():
    """notebooklm._research.CitedSourceSelection continues to resolve."""
    from notebooklm._research import CitedSourceSelection as Legacy
    from notebooklm.types import CitedSourceSelection

    assert Legacy is CitedSourceSelection


# ---------------------------------------------------------------------------
# PR-T4.2 section: RPC enums re-exported via notebooklm.types
#
# CLI modules import these enums from ``notebooklm.types`` (the public surface)
# rather than reaching into ``notebooklm.rpc`` directly. The re-exports must be
# the exact same objects as the canonical definitions in ``notebooklm.rpc.types``
# (identity, not just equality), so isinstance checks and equality both work
# regardless of which import path callers use.
#
# The explicit list below covers every public RPC enum re-exported by
# ``notebooklm.types`` (see ``notebooklm.types.__all__``). Keep this list in
# sync with the re-exports so any accidental shadowing in ``types.py`` —
# redefining instead of re-exporting — is caught immediately. ``ArtifactTypeCode``
# is intentionally excluded because it is imported by ``types.py`` for internal
# use but not part of the public ``__all__``.
# ---------------------------------------------------------------------------


_REEXPORTED_RPC_ENUMS = [
    "ArtifactStatus",
    "AudioFormat",
    "AudioLength",
    "ChatGoal",
    "ChatResponseLength",
    "DriveMimeType",
    "ExportType",
    "InfographicDetail",
    "InfographicOrientation",
    "InfographicStyle",
    "QuizDifficulty",
    "QuizQuantity",
    "ReportFormat",
    "ShareAccess",
    "SharePermission",
    "ShareViewLevel",
    "SlideDeckFormat",
    "SlideDeckLength",
    "SourceStatus",
    "VideoFormat",
    "VideoStyle",
]


@pytest.mark.parametrize("enum_name", _REEXPORTED_RPC_ENUMS)
def test_rpc_enum_reexports_are_identical(enum_name: str) -> None:
    """notebooklm.types.<Enum> is the same object as notebooklm.rpc.types.<Enum>."""
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    public_enum = getattr(public_types, enum_name)
    canonical_enum = getattr(rpc_types, enum_name)
    assert public_enum is canonical_enum, (
        f"notebooklm.types.{enum_name} must be the same object as "
        f"notebooklm.rpc.types.{enum_name} (identity, not equality)"
    )


def test_rpc_enum_reexport_list_matches_public_all() -> None:
    """The _REEXPORTED_RPC_ENUMS guard list must stay aligned with notebooklm.types.__all__.

    If a new enum is re-exported in ``types.py``'s ``__all__`` but not added
    here, this test fails — preventing silent gaps in the identity coverage.
    """
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    # Names that appear in both __all__ and rpc.types — i.e. the actual
    # re-exported RPC enums.
    declared = set(public_types.__all__)
    rpc_names = {name for name in dir(rpc_types) if not name.startswith("_")}
    expected = declared & rpc_names
    # Drop helper functions (not enums) from the comparison.
    expected -= {"artifact_status_to_str", "source_status_to_str"}

    listed = set(_REEXPORTED_RPC_ENUMS)
    missing = expected - listed
    extras = listed - expected
    assert not missing, (
        f"_REEXPORTED_RPC_ENUMS is missing newly re-exported enum(s): {sorted(missing)}"
    )
    assert not extras, (
        f"_REEXPORTED_RPC_ENUMS contains name(s) no longer re-exported: {sorted(extras)}"
    )


# ---------------------------------------------------------------------------
# PR-D section: notebooklm.config / notebooklm.urls / notebooklm.log public shims
# ---------------------------------------------------------------------------


def test_config_shim_exposes_documented_names(monkeypatch):
    # Guard against a NOTEBOOKLM_BASE_URL override leaking from the env,
    # so the assertion stays valid on developer machines and overridden CI.
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    from notebooklm import config

    assert config.get_base_url() == config.DEFAULT_BASE_URL
    assert config.DEFAULT_BASE_URL == "https://notebooklm.google.com"


def test_urls_shim_exposes_documented_names():
    from notebooklm.urls import is_youtube_url

    assert is_youtube_url("https://www.youtube.com/watch?v=x") is True


def test_log_shim_exposes_install_redaction():
    from notebooklm.log import install_redaction

    assert callable(install_redaction)


# ---------------------------------------------------------------------------
# PR-T1 API contract section: public raw-RPC and documented facade imports
# ---------------------------------------------------------------------------


def test_rpc_method_uses_documented_power_user_import_path() -> None:
    """Raw-RPC examples use notebooklm.rpc.RPCMethod, not notebooklm.types."""
    from notebooklm.rpc import RPCMethod
    from notebooklm.rpc.types import RPCMethod as CanonicalRPCMethod

    assert RPCMethod is CanonicalRPCMethod


def test_rpc_method_is_not_reexported_from_notebooklm_types() -> None:
    """RPCMethod is intentionally not part of notebooklm.types in this phase."""
    import notebooklm.types as public_types

    assert "RPCMethod" not in public_types.__all__
    assert not hasattr(public_types, "RPCMethod")


def test_auth_cookie_domain_constants_are_facade_exports() -> None:
    """Cookie-domain tiers remain importable from notebooklm.auth."""
    from notebooklm.auth import (
        OPTIONAL_COOKIE_DOMAINS,
        OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
        REQUIRED_COOKIE_DOMAINS,
    )

    assert isinstance(REQUIRED_COOKIE_DOMAINS, frozenset)
    assert isinstance(OPTIONAL_COOKIE_DOMAINS, frozenset)
    assert isinstance(OPTIONAL_COOKIE_DOMAINS_BY_LABEL, dict)
    assert frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values()) == OPTIONAL_COOKIE_DOMAINS


# ---------------------------------------------------------------------------
# PR-T3A section: notebooklm.auth first-party compatibility surface
#
# This is narrower than a future public API decision. It only freezes the names
# that current first-party modules, CLI code, tests, and docs may rely on while
# Phase 2 is free to move auth internals underneath ``notebooklm._auth``.
# Removing one of these names from ``notebooklm.auth`` requires a separate
# deprecation/migration plan, not an internal-module move PR.
#
# Underscored entries are compatibility-only for non-CLI first-party callers;
# the CLI boundary test still forbids CLI modules from importing private names
# out of ``notebooklm.auth``. Other auth names, such as ``flatten_cookie_map``,
# are intentionally outside this enforced move-safety manifest unless added by
# a separate public or first-party compatibility decision.
# ---------------------------------------------------------------------------


_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES = [
    "_auth_domain_priority",
    "_EXTRACTION_HINT",
    "_find_cookie_for_storage",
    "_has_valid_secondary_binding",
    "_is_allowed_auth_domain",
    "_is_allowed_cookie_domain",
    "_is_google_domain",
    "_rotate_cookies",
    "_run_refresh_cmd",
    "_SECONDARY_BINDING_WARNED",
    "_split_refresh_cmd",
    "_update_cookie_input",
    "_validate_required_cookies",
    "Account",
    "advance_cookie_snapshot_after_save",
    "ALLOWED_COOKIE_DOMAINS",
    "authuser_query",
    "AuthTokens",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "clear_account_metadata",
    "convert_rookiepy_cookies_to_storage_state",
    "CookieSaveResult",
    "CookieSnapshot",
    "CookieSnapshotKey",
    "CookieSnapshotValue",
    "enumerate_accounts",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "extract_csrf_from_html",
    "extract_email_from_html",
    "extract_session_id_from_html",
    "extract_wiz_field",
    "fetch_tokens",
    "fetch_tokens_with_domains",
    "format_authuser_value",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "GOOGLE_REGIONAL_CCTLDS",
    "KEEPALIVE_ROTATE_URL",
    "load_auth_from_storage",
    "load_httpx_cookies",
    "MINIMUM_REQUIRED_COOKIES",
    "normalize_cookie_map",
    "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV",
    "NOTEBOOKLM_REFRESH_CMD_ENV",
    "NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "read_account_metadata",
    "REQUIRED_COOKIE_DOMAINS",
    "save_cookies_to_storage",
    "snapshot_cookie_jar",
    "write_account_metadata",
]


@pytest.mark.parametrize("name", _AUTH_FIRST_PARTY_COMPATIBILITY_NAMES)
def test_auth_first_party_compatibility_manifest_resolves(name: str) -> None:
    """Phase 2 internals may move, but first-party callers keep notebooklm.auth."""
    import notebooklm.auth as auth

    assert hasattr(auth, name), f"notebooklm.auth.{name} disappeared"


def test_auth_first_party_compatibility_manifest_has_no_duplicates() -> None:
    """The enforced compatibility manifest should stay reviewable."""
    assert len(_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES) == len(
        set(_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES)
    )


def test_auth_cookie_policy_facade_delegates_to_private_module() -> None:
    """Policy constants/helpers live in _auth while notebooklm.auth stays compatible."""
    import notebooklm.auth as auth
    from notebooklm._auth import cookie_policy

    assert auth.REQUIRED_COOKIE_DOMAINS is cookie_policy.REQUIRED_COOKIE_DOMAINS
    assert auth.OPTIONAL_COOKIE_DOMAINS is cookie_policy.OPTIONAL_COOKIE_DOMAINS
    assert auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL is cookie_policy.OPTIONAL_COOKIE_DOMAINS_BY_LABEL
    assert auth.ALLOWED_COOKIE_DOMAINS is cookie_policy.ALLOWED_COOKIE_DOMAINS
    assert auth.GOOGLE_REGIONAL_CCTLDS is cookie_policy.GOOGLE_REGIONAL_CCTLDS
    assert auth.MINIMUM_REQUIRED_COOKIES is cookie_policy.MINIMUM_REQUIRED_COOKIES
    assert auth._auth_domain_priority is cookie_policy._auth_domain_priority
    assert auth._is_google_domain is cookie_policy._is_google_domain
    assert auth._is_allowed_auth_domain is cookie_policy._is_allowed_auth_domain
    assert auth._is_allowed_cookie_domain is cookie_policy._is_allowed_cookie_domain


def test_auth_secondary_binding_reset_syncs_to_cookie_policy(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resetting notebooklm.auth._SECONDARY_BINDING_WARNED still controls validation."""
    import notebooklm.auth as auth
    from notebooklm._auth import cookie_policy

    monkeypatch.setattr(cookie_policy, "_SECONDARY_BINDING_WARNED", True)
    monkeypatch.setattr(auth, "_SECONDARY_BINDING_WARNED", False)

    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        auth._validate_required_cookies({"SID", "__Secure-1PSIDTS"})

    assert auth._SECONDARY_BINDING_WARNED is True
    assert cookie_policy._SECONDARY_BINDING_WARNED is True
    assert "Cookie set lacks a secondary binding" in caplog.text


def test_auth_validation_preserves_private_warning_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facade validation must not clobber a private validation's warning state."""
    import notebooklm.auth as auth
    from notebooklm._auth import cookie_policy

    monkeypatch.setattr(auth, "_SECONDARY_BINDING_WARNED", False)
    cookie_policy._validate_required_cookies({"SID", "__Secure-1PSIDTS"})

    auth._validate_required_cookies({"SID", "__Secure-1PSIDTS", "OSID"})

    assert auth._SECONDARY_BINDING_WARNED is True
    assert cookie_policy._SECONDARY_BINDING_WARNED is True


def test_auth_validation_uses_facade_policy_rebindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation keeps auth.py monkeypatch compatibility after delegation."""
    import notebooklm.auth as auth

    monkeypatch.setattr(auth, "MINIMUM_REQUIRED_COOKIES", {"SID"})
    monkeypatch.setattr(auth, "_has_valid_secondary_binding", lambda names: True)

    auth._validate_required_cookies({"SID"})


def test_auth_validation_uses_facade_extraction_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier-1 errors still read the compatibility facade's extraction hint."""
    import notebooklm.auth as auth

    monkeypatch.setattr(auth, "MINIMUM_REQUIRED_COOKIES", {"SID", "SIDTS"})
    monkeypatch.setattr(auth, "_EXTRACTION_HINT", "custom extraction hint")

    with pytest.raises(ValueError, match="custom extraction hint"):
        auth._validate_required_cookies({"SID"})


@pytest.mark.asyncio
async def test_client_rpc_call_delegates_keyword_for_keyword() -> None:
    """NotebookLMClient.rpc_call is a public delegator to ClientCore.rpc_call."""
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.rpc import RPCMethod

    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    client._core.rpc_call = AsyncMock(return_value={"ok": True})

    result = await client.rpc_call(
        RPCMethod.CREATE_NOTEBOOK,
        ["My Notebook"],
        source_path="/notebook/abc",
        allow_null=True,
        _is_retry=True,
        disable_internal_retries=True,
    )

    assert result == {"ok": True}
    client._core.rpc_call.assert_awaited_once_with(
        method=RPCMethod.CREATE_NOTEBOOK,
        params=["My Notebook"],
        source_path="/notebook/abc",
        allow_null=True,
        _is_retry=True,
        disable_internal_retries=True,
    )


@pytest.mark.asyncio
async def test_client_rpc_call_forwards_default_arguments() -> None:
    """The public delegator must preserve ClientCore.rpc_call defaults."""
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.rpc import RPCMethod

    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    # No async context is needed: this test replaces the core RPC coroutine
    # before any real transport initialization can be required.
    client._core.rpc_call = AsyncMock(return_value=[])

    result = await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    assert result == []
    client._core.rpc_call.assert_awaited_once_with(
        method=RPCMethod.LIST_NOTEBOOKS,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
    )


# ---------------------------------------------------------------------------
# PR-T1.D section: __all__ contract tests for the public shim modules.
#
# Enforces, for each shim, that:
#   1. ``__all__`` exists.
#   2. Every name in ``__all__`` resolves via ``getattr``.
#   3. No name in ``__all__`` is private (leading underscore).
#   4. ``__all__`` is sorted case-insensitively (drift catcher).
#   5. ``__all__`` matches the actual re-exported public surface — no orphans,
#      no missing entries.
#   6. ``__all__`` contains no duplicate entries.
# ---------------------------------------------------------------------------


# (shim_module_name, internal_module_name)
# Note: notebooklm.research has targeted smoke tests in the PR-A section above
# and is intentionally excluded from this generic contract sweep.
_SHIM_PAIRS = [
    ("notebooklm.config", "notebooklm._env"),
    ("notebooklm.urls", "notebooklm._url_utils"),
    ("notebooklm.log", "notebooklm._logging"),
]


def _actual_reexports(shim: ModuleType, internal: ModuleType) -> set[str]:
    """Return public names on ``shim`` that point at the same object on ``internal``.

    A name is considered "re-exported" when both modules expose an attribute
    of the same identity. This catches accidental shadowing (a shim defining
    its own value) as well as truly re-exported symbols.

    Note: names imported under ``typing.TYPE_CHECKING`` are not visible to
    ``dir()`` at runtime, so type-only re-exports won't be detected. None of
    the current shims use TYPE_CHECKING re-exports.
    """
    sentinel = object()
    names: set[str] = set()
    for name in dir(shim):
        if name.startswith("_"):
            continue
        shim_obj = getattr(shim, name, sentinel)
        internal_obj = getattr(internal, name, sentinel)
        if shim_obj is sentinel or internal_obj is sentinel:
            continue
        if shim_obj is internal_obj:
            names.add(name)
    return names


@pytest.mark.parametrize(
    ("shim_name", "internal_name"),
    _SHIM_PAIRS,
    ids=[shim for shim, _ in _SHIM_PAIRS],
)
def test_public_shim_all_contract(shim_name: str, internal_name: str) -> None:
    shim = importlib.import_module(shim_name)
    internal = importlib.import_module(internal_name)

    # 1. __all__ exists.
    assert hasattr(shim, "__all__"), f"{shim_name} is missing __all__"
    all_list = shim.__all__
    assert isinstance(all_list, list), (
        f"{shim_name}.__all__ must be a list, got {type(all_list).__name__}"
    )

    # 2. Every name in __all__ is importable.
    for name in all_list:
        assert hasattr(shim, name), f"{shim_name}.__all__ references missing attribute {name!r}"

    # 3. No private names in __all__.
    private = [n for n in all_list if n.startswith("_")]
    assert not private, f"{shim_name}.__all__ leaks private names: {private}"

    # 4. __all__ sorted case-insensitively (drift catcher).
    expected_order = sorted(all_list, key=str.lower)
    assert list(all_list) == expected_order, (
        f"{shim_name}.__all__ is not sorted case-insensitively.\n"
        f"  actual:   {list(all_list)}\n"
        f"  expected: {expected_order}"
    )

    # 5. __all__ matches the actual public surface of the shim.
    declared = set(all_list)
    reexported = _actual_reexports(shim, internal)
    missing = reexported - declared
    orphans = declared - reexported
    assert not missing, (
        f"{shim_name}.__all__ is missing names re-exported from {internal_name}: {sorted(missing)}"
    )
    assert not orphans, (
        f"{shim_name}.__all__ contains orphans not re-exported from {internal_name}: "
        f"{sorted(orphans)}"
    )

    # 6. Length sanity: no duplicates in __all__.
    assert len(all_list) == len(declared), (
        f"{shim_name}.__all__ contains duplicates: {sorted(all_list)}"
    )
