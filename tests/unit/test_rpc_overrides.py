"""Unit tests for ``NOTEBOOKLM_RPC_OVERRIDES`` self-patch escape hatch.

The override mechanism lets users self-patch when Google rotates an
obfuscated batchexecute method id, *without* waiting for a release. The
critical invariant: the resolved id must flow into BOTH the URL's
``rpcids=`` query param AND the request body's ``f.req`` payload —
mismatched ids reach the wire as malformed requests.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod
from notebooklm.rpc import overrides as rpc_overrides
from notebooklm.rpc import types as rpc_types
from notebooklm.rpc.overrides import _load_rpc_overrides, _parse_rpc_overrides, resolve_rpc_id


@pytest.fixture(autouse=True)
def _clear_override_caches():
    """Clear parser cache + INFO-dedup set between tests so warnings reproduce."""
    _parse_rpc_overrides.cache_clear()
    rpc_overrides._logged_override_hashes.clear()
    yield
    _parse_rpc_overrides.cache_clear()
    rpc_overrides._logged_override_hashes.clear()


def test_rpc_overrides_direct_smoke_import() -> None:
    """The new private owner module exposes the runtime resolver and cached parser."""
    from notebooklm.rpc.overrides import _parse_rpc_overrides, resolve_rpc_id

    assert callable(resolve_rpc_id)
    assert hasattr(_parse_rpc_overrides, "cache_clear")


def test_rpc_types_override_aliases_are_identity_compatible() -> None:
    """Legacy private imports from rpc.types must keep pointing at the new owner objects."""
    assert rpc_types.resolve_rpc_id is rpc_overrides.resolve_rpc_id
    assert rpc_types._parse_rpc_overrides is rpc_overrides._parse_rpc_overrides
    assert rpc_types._load_rpc_overrides is rpc_overrides._load_rpc_overrides
    assert rpc_types._logged_override_hashes is rpc_overrides._logged_override_hashes


# ---------------------------------------------------------------------------
# _load_rpc_overrides — env-var parsing
# ---------------------------------------------------------------------------


def test_load_rpc_overrides_unset(monkeypatch):
    """Unset env var → empty dict, no warning."""
    monkeypatch.delenv("NOTEBOOKLM_RPC_OVERRIDES", raising=False)
    assert _load_rpc_overrides() == {}


def test_load_rpc_overrides_empty_string(monkeypatch):
    """Empty env var → empty dict (treated as unset)."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", "")
    assert _load_rpc_overrides() == {}


def test_load_rpc_overrides_valid_json(monkeypatch):
    """Valid JSON object → parsed dict of string→string."""
    monkeypatch.setenv(
        "NOTEBOOKLM_RPC_OVERRIDES",
        '{"LIST_NOTEBOOKS": "AbCdEf", "CREATE_NOTEBOOK": "GhIjKl"}',
    )
    assert _load_rpc_overrides() == {
        "LIST_NOTEBOOKS": "AbCdEf",
        "CREATE_NOTEBOOK": "GhIjKl",
    }


def test_load_rpc_overrides_invalid_json_warns(monkeypatch, caplog):
    """Malformed JSON → WARNING logged, empty dict returned."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", "{not-valid")
    with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
        result = _load_rpc_overrides()
    assert result == {}
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_load_rpc_overrides_non_dict_warns(monkeypatch, caplog):
    """Non-dict top-level (array) → WARNING logged, empty dict returned."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '["LIST_NOTEBOOKS", "AbCdEf"]')
    with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
        result = _load_rpc_overrides()
    assert result == {}
    assert any("must be a JSON object" in r.message for r in caplog.records)


def test_load_rpc_overrides_string_top_level_warns(monkeypatch, caplog):
    """Top-level JSON string (also non-dict) → WARNING + empty dict."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '"just_a_string"')
    with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
        result = _load_rpc_overrides()
    assert result == {}
    assert any("must be a JSON object" in r.message for r in caplog.records)


def test_load_rpc_overrides_coerces_values_to_str(monkeypatch):
    """Non-string values in the override map are coerced to str (defensive)."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": 12345}')
    assert _load_rpc_overrides() == {"LIST_NOTEBOOKS": "12345"}


def test_load_rpc_overrides_drops_null_values_with_warning(monkeypatch, caplog):
    """JSON ``null`` values are dropped + warned, never coerced to ``"None"``.

    Without this gate, ``str(None)`` would put the literal four-character
    string ``"None"`` on the wire as the override RPC id — almost certainly
    not what the user intended.
    """
    monkeypatch.setenv(
        "NOTEBOOKLM_RPC_OVERRIDES",
        '{"LIST_NOTEBOOKS": null, "CREATE_NOTEBOOK": "valid"}',
    )
    with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
        result = _load_rpc_overrides()
    assert result == {"CREATE_NOTEBOOK": "valid"}
    assert "LIST_NOTEBOOKS" not in result
    assert any("null values" in r.message and "LIST_NOTEBOOKS" in r.message for r in caplog.records)


def test_load_rpc_overrides_drops_unknown_keys_with_warning(monkeypatch, caplog):
    """Keys not matching an RPCMethod member are dropped + warned, not silently kept.

    Without this gate, a typo (``"LIST_NOTEBOOK"``) would silently no-op
    while the INFO line still claimed the override was applied — exactly
    the failure mode the escape hatch is supposed to prevent.
    """
    monkeypatch.setenv(
        "NOTEBOOKLM_RPC_OVERRIDES",
        '{"LIST_NOTEBOOK": "typo", "LIST_NOTEBOOKS": "real"}',
    )
    with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
        result = _load_rpc_overrides()
    assert result == {"LIST_NOTEBOOKS": "real"}
    assert "LIST_NOTEBOOK" not in result
    assert any(
        "Ignoring unknown" in r.message and "LIST_NOTEBOOK" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# resolve_rpc_id — host-gate + override application
# ---------------------------------------------------------------------------


def test_resolve_rpc_id_no_env_var_returns_canonical(monkeypatch):
    """With no env var set, resolve returns the canonical id verbatim."""
    monkeypatch.delenv("NOTEBOOKLM_RPC_OVERRIDES", raising=False)
    rpc_overrides._logged_override_hashes.clear()
    assert (
        resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
        == RPCMethod.LIST_NOTEBOOKS.value
    )


def test_resolve_rpc_id_with_override(monkeypatch):
    """An override mapped to the method name replaces the canonical id."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "NEW_ID_v2"}')
    rpc_overrides._logged_override_hashes.clear()
    assert resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value) == "NEW_ID_v2"


def test_resolve_rpc_id_unknown_method_unaffected(monkeypatch):
    """Override map entry for a method we never call → no impact on other calls."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"SOME_RENAMED_METHOD": "x9x9x9"}')
    rpc_overrides._logged_override_hashes.clear()
    assert (
        resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
        == RPCMethod.LIST_NOTEBOOKS.value
    )


def test_resolve_rpc_id_host_not_allowlisted_ignores_override(monkeypatch):
    """Host gate: if get_base_host() returns a non-allowed host, overrides are dropped."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "shouldNOTApply"}')
    rpc_overrides._logged_override_hashes.clear()
    # The host gate uses ``_env.get_base_host`` — patch it inline so we don't
    # depend on a real off-allowlist URL (which the validator would reject).
    monkeypatch.setattr("notebooklm._env.get_base_host", lambda: "evil.example.com")
    assert (
        resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
        == RPCMethod.LIST_NOTEBOOKS.value
    )


def test_resolve_rpc_id_host_resolver_raises_falls_back(monkeypatch):
    """If get_base_host() raises (malformed env), override is silently skipped."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "shouldNOTApply"}')
    rpc_overrides._logged_override_hashes.clear()

    def _boom() -> str:
        raise ValueError("malformed NOTEBOOKLM_BASE_URL")

    monkeypatch.setattr("notebooklm._env.get_base_host", _boom)
    assert (
        resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
        == RPCMethod.LIST_NOTEBOOKS.value
    )


def test_resolve_rpc_id_enterprise_host_allowed(monkeypatch):
    """The enterprise host (notebooklm.cloud.google.com) also passes the gate."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "ENT_ID"}')
    rpc_overrides._logged_override_hashes.clear()
    monkeypatch.setattr("notebooklm._env.get_base_host", lambda: "notebooklm.cloud.google.com")
    assert resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value) == "ENT_ID"


def test_resolve_rpc_id_logs_once_per_unique_set(monkeypatch, caplog):
    """A given override mapping is logged at INFO exactly once per distinct set."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "X"}')
    rpc_overrides._logged_override_hashes.clear()
    with caplog.at_level("INFO", logger="notebooklm.rpc.overrides"):
        for _ in range(5):
            resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
    info_lines = [r for r in caplog.records if r.levelname == "INFO" and "OVERRIDES" in r.message]
    assert len(info_lines) == 1


def test_resolve_rpc_id_logs_again_for_different_set(monkeypatch, caplog):
    """Two distinct override sets each emit one INFO line."""
    rpc_overrides._logged_override_hashes.clear()
    with caplog.at_level("INFO", logger="notebooklm.rpc.overrides"):
        monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "v1"}')
        resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
        monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "v2"}')
        resolve_rpc_id("LIST_NOTEBOOKS", RPCMethod.LIST_NOTEBOOKS.value)
    info_lines = [r for r in caplog.records if r.levelname == "INFO" and "OVERRIDES" in r.message]
    assert len(info_lines) == 2


# ---------------------------------------------------------------------------
# encode_rpc_request — rpc_id_override threading
# ---------------------------------------------------------------------------


def test_encode_rpc_request_default_uses_canonical():
    """No override → ``method.value`` is embedded in the request body."""
    from notebooklm.rpc.encoder import encode_rpc_request

    result = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, [None, 1])
    assert result[0][0][0] == RPCMethod.LIST_NOTEBOOKS.value


def test_encode_rpc_request_override_replaces_id():
    """When ``rpc_id_override`` is provided, the override is embedded instead."""
    from notebooklm.rpc.encoder import encode_rpc_request

    result = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, [None, 1], rpc_id_override="OVERRIDE_v9")
    assert result[0][0][0] == "OVERRIDE_v9"


def test_encode_rpc_request_none_override_uses_canonical():
    """Explicit None override → falls back to canonical id (no surprise)."""
    from notebooklm.rpc.encoder import encode_rpc_request

    result = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, [None, 1], rpc_id_override=None)
    assert result[0][0][0] == RPCMethod.LIST_NOTEBOOKS.value


# ---------------------------------------------------------------------------
# Integration: both call sites must use the SAME resolved id
# ---------------------------------------------------------------------------


def _make_core() -> ClientCore:
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "sid_cookie"},
    )
    return ClientCore(
        auth=auth,
        refresh_callback=None,
        refresh_retry_delay=0.0,
    )


def _ok_response_for(rpc_id: str) -> httpx.Response:
    inner = json.dumps([])
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    text = f")]}}'\n{len(chunk)}\n{chunk}\n"
    return httpx.Response(
        200,
        text=text,
        request=httpx.Request("POST", "https://example.test/x"),
    )


@pytest.mark.parametrize(
    ("env_value", "expected_id"),
    [
        # No env var → canonical id at both sites.
        pytest.param(None, RPCMethod.LIST_NOTEBOOKS.value, id="no-env"),
        # One override for the called method → override flows to both sites.
        pytest.param(
            '{"LIST_NOTEBOOKS": "PATCHED_v2"}',
            "PATCHED_v2",
            id="override-applied",
        ),
        # Override for a DIFFERENT method → called method still uses canonical.
        pytest.param(
            '{"CREATE_NOTEBOOK": "noTouch"}',
            RPCMethod.LIST_NOTEBOOKS.value,
            id="override-for-other-method",
        ),
    ],
)
@pytest.mark.asyncio
async def test_rpc_call_resolved_id_at_both_sites(monkeypatch, env_value, expected_id):
    """The resolved RPC id is consistent across URL ``rpcids=`` AND body ``f.req``.

    A one-site resolver would send mismatched ids on the wire, which is the
    failure mode that motivated the v2 plan's "both sites" critical-scope
    correction.
    """
    if env_value is None:
        monkeypatch.delenv("NOTEBOOKLM_RPC_OVERRIDES", raising=False)
    else:
        monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", env_value)
    rpc_overrides._logged_override_hashes.clear()

    core = _make_core()
    await core.open()
    try:
        captured: dict[str, Any] = {}

        async def fake_post(url, *, content, **kwargs):
            captured["url"] = url
            captured["content"] = content
            # Server echoes back whatever rpc id we sent — that's the contract
            # decode_response relies on. Use the EXPECTED id so the test
            # exercises the full encode → wire → decode round-trip.
            return _ok_response_for(expected_id)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [None, 1])

        # URL site
        assert f"rpcids={expected_id}" in captured["url"]
        # Body site — f.req is URL-encoded JSON, find the inner id as a quoted token.
        assert f'"{expected_id}"' in httpx.QueryParams(captured["content"])["f.req"]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_rpc_call_host_off_allowlist_ignores_override(monkeypatch):
    """When ``get_base_host()`` is off the allowlist, overrides are inert.

    Defense-in-depth: the strict ``get_base_url()`` validator already rejects
    off-allowlist hosts at import time, but the resolver re-checks so a
    monkeypatched env can't leak custom RPC ids to a hostile endpoint.
    """
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '{"LIST_NOTEBOOKS": "shouldNOTApply"}')
    monkeypatch.setattr("notebooklm._env.get_base_host", lambda: "evil.example.com")
    rpc_overrides._logged_override_hashes.clear()

    core = _make_core()
    await core.open()
    try:
        captured: dict[str, Any] = {}

        async def fake_post(url, *, content, **kwargs):
            captured["url"] = url
            captured["content"] = content
            return _ok_response_for(RPCMethod.LIST_NOTEBOOKS.value)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [None, 1])

        assert f"rpcids={RPCMethod.LIST_NOTEBOOKS.value}" in captured["url"]
        assert "shouldNOTApply" not in captured["url"]
        assert "shouldNOTApply" not in captured["content"]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_rpc_call_invalid_json_falls_back_with_warning(monkeypatch, caplog):
    """Invalid JSON in env var → WARNING + canonical ids on the wire."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", "{not-json")
    rpc_overrides._logged_override_hashes.clear()

    core = _make_core()
    await core.open()
    try:
        captured: dict[str, Any] = {}

        async def fake_post(url, *, content, **kwargs):
            captured["url"] = url
            captured["content"] = content
            return _ok_response_for(RPCMethod.LIST_NOTEBOOKS.value)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [None, 1])

        assert any("not valid JSON" in r.message for r in caplog.records)
        assert f"rpcids={RPCMethod.LIST_NOTEBOOKS.value}" in captured["url"]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_rpc_call_non_dict_json_falls_back_with_warning(monkeypatch, caplog):
    """Top-level array → WARNING + canonical ids on the wire."""
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", '["LIST_NOTEBOOKS", "ignored"]')
    rpc_overrides._logged_override_hashes.clear()

    core = _make_core()
    await core.open()
    try:
        captured: dict[str, Any] = {}

        async def fake_post(url, *, content, **kwargs):
            captured["url"] = url
            captured["content"] = content
            return _ok_response_for(RPCMethod.LIST_NOTEBOOKS.value)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with caplog.at_level("WARNING", logger="notebooklm.rpc.overrides"):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [None, 1])

        assert any("must be a JSON object" in r.message for r in caplog.records)
        assert f"rpcids={RPCMethod.LIST_NOTEBOOKS.value}" in captured["url"]
    finally:
        await core.close()
