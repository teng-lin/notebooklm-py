"""Derivations for the semantic-refactor compatibility baselines.

The baselines in this module freeze contracts that the release-to-release
public API audit does not fully describe:

* structural and pickle identity for every public dataclass and enum;
* the public metrics field vocabulary and composed logical-RPC event behavior; and
* per-channel ``to_jsonable`` field-key schemas for CLI, MCP, and REST.

All inventories come from the ``__all__`` surfaces of the public modules
discovered by ``scripts/audit_public_api_compat.py`` and live dataclass
metadata.  There is no hand-maintained model list to go stale.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import importlib
import inspect
import pickle
import types
import typing
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .json_envelope_contracts import (
    _evidence_ast_fingerprint,
    _secret_serialization_violations,
    _validate_no_secret_channel_models,
    derive_json_envelope_contract,
)


def _audit_public_modules() -> list[str]:
    """Return the public modules discovered by the compatibility audit."""
    import scripts.audit_public_api_compat as audit

    import notebooklm

    package_dir = Path(notebooklm.__file__).resolve().parent
    modules = {audit.PUBLIC_PACKAGE}
    for path in package_dir.glob("*.py"):
        stem = path.stem
        if stem.startswith("_") or stem in audit.EXCLUDED_TOP_LEVEL_MODULES:
            continue
        modules.add(f"{audit.PUBLIC_PACKAGE}.{stem}")
    for name in audit.EXTRA_PUBLIC_PACKAGES:
        if (package_dir / name / "__init__.py").is_file():
            modules.add(f"{audit.PUBLIC_PACKAGE}.{name}")
    return sorted(modules)


def _public_model_exports() -> dict[type[Any], list[str]]:
    """Return every dataclass/enum exported by every audited public module.

    Values are deduplicated by class identity while retaining every public
    export path that reaches the identity.  This includes compatibility
    re-exports such as ``notebooklm.AuthTokens`` /
    ``notebooklm.auth.AuthTokens`` without snapshotting the class twice.
    """
    models: dict[type[Any], list[str]] = {}
    for module_name in _audit_public_modules():
        module = importlib.import_module(module_name)
        for name in getattr(module, "__all__", ()):
            value = getattr(module, name)
            if not isinstance(value, type):
                continue
            if not (dataclasses.is_dataclass(value) or issubclass(value, enum.Enum)):
                continue
            models.setdefault(value, []).append(f"{module_name}.{name}")
    return models


def _model_key(cls: type[Any]) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _annotation_repr(annotation: object) -> str:
    """Render postponed and live annotations without resolving facade globals.

    Several public classes intentionally rewrite ``__module__`` to
    ``notebooklm.types`` for pickle compatibility.  Resolving all hints through
    that facade can therefore fail for defining-module-only names.  The raw
    dataclass field annotation is the stable contract here.
    """
    if isinstance(annotation, str):
        return annotation
    return inspect.formatannotation(annotation)


def _declaring_owner(cls: type[Any], method_name: str) -> type[Any]:
    return next(base for base in cls.__mro__ if method_name in base.__dict__)


def _method_policy(cls: type[Any], method_name: str) -> dict[str, object]:
    """Describe equality/hash/repr semantics without pinning code locations."""
    owner = _declaring_owner(cls, method_name)
    method = owner.__dict__[method_name]
    if method is None:
        return {"policy": "disabled"}
    if owner is not cls:
        return {
            "policy": "inherited",
            "owner": f"{owner.__module__}.{owner.__qualname__}",
        }

    target = inspect.unwrap(method)
    code = getattr(target, "__code__", None)
    policy = "dataclass-generated" if getattr(code, "co_filename", None) == "<string>" else "custom"
    return {"policy": policy}


def _sample_value(annotation: object, field_name: str, stack: tuple[type[Any], ...]) -> object:
    """Return a small valid value for one required constructor parameter."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if annotation is str:
        return "contract"
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    if annotation is bool:
        return False
    if annotation is bytes:
        return b"contract"
    if annotation is Any:
        return None
    if origin is typing.Literal:
        return args[0]
    if origin in (typing.Union, types.UnionType):
        if type(None) in args:
            return None
        return _sample_value(args[0], field_name, stack)
    if origin is list:
        return []
    if origin is tuple:
        return ()
    if origin is dict or origin is Mapping:
        return {}
    if origin is set:
        return set()
    if origin is frozenset:
        return frozenset()
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return next(iter(annotation))
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        if annotation in stack:
            return None
        return _valid_dataclass_sample(annotation, stack=stack)
    if field_name == "status":
        return "success"
    return None


def _valid_dataclass_sample(cls: type[Any], *, stack: tuple[type[Any], ...] = ()) -> object:
    """Construct a valid public instance through its real ``__init__`` path."""
    try:
        hints = typing.get_type_hints(cls.__init__)
    except (NameError, TypeError):
        hints = {}
    kwargs: dict[str, object] = {}
    for field in dataclasses.fields(cls):
        if not field.init:
            continue
        if field.default is not dataclasses.MISSING:
            continue
        if field.default_factory is not dataclasses.MISSING:
            continue
        annotation = hints.get(field.name, field.type)
        kwargs[field.name] = _sample_value(annotation, field.name, (*stack, cls))
    return cls(**kwargs)


def _pickle_contract(value: object, *, identity: bool) -> dict[str, object]:
    """Characterize real pickle behavior without turning failure into a crash."""
    try:
        payload = pickle.dumps(value)
    except Exception as exc:  # noqa: BLE001 - failure is the measured contract
        category = (
            "unpickleable-thread-lock"
            if isinstance(exc, TypeError) and "RLock" in str(exc)
            else "other"
        )
        return {
            "status": "failure",
            "stage": "dumps",
            "error_type": type(exc).__qualname__,
            "error_category": category,
        }
    try:
        restored = pickle.loads(payload)
    except Exception as exc:  # noqa: BLE001 - failure is the measured contract
        return {
            "status": "failure",
            "stage": "loads",
            "error_type": type(exc).__qualname__,
            "error_category": "other",
        }
    matched = restored is value if identity else restored == value
    return {
        "status": "success" if matched else "mismatch",
        "comparison": "identity" if identity else "equality",
        "restored_type": _model_key(type(restored)),
    }


def _state_hook_contract(cls: type[Any], method_name: str) -> dict[str, object]:
    """Record first-party pickle-state hooks, ignoring interpreter-added object hooks."""
    for owner in cls.__mro__:
        if method_name not in owner.__dict__:
            continue
        if owner is object or not owner.__module__.startswith("notebooklm"):
            return {"present": False}
        return {
            "present": True,
            "owner": f"{owner.__module__}.{owner.__qualname__}",
        }
    return {"present": False}


def _legacy_state_contract(cls: type[Any]) -> dict[str, object] | None:
    """Exercise the two public legacy-state migrations protected by ``__setstate__``."""
    key = _model_key(cls)
    if key == "notebooklm.types.Notebook":
        timestamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        current = cls(id="contract-notebook", title="Contract", modified_at=timestamp)
        state = dict(current.__dict__)
        state.pop("last_viewed_at", None)
        state.pop("chat_sessions", None)
        expected = {
            "last_viewed_at_restored": timestamp,
            "chat_sessions_restored": [],
        }
    elif key == "notebooklm.types.ChatReference":
        current = cls(
            source_id="contract-source",
            answer_start_char=3,
            answer_end_char=8,
        )
        state = dict(current.__dict__)
        state.pop("fragment_start_char", None)
        state.pop("fragment_end_char", None)
        expected = {
            "fragment_start_char_restored": 3,
            "fragment_end_char_restored": 8,
            "answer_start_char_mirrored": 3,
            "answer_end_char_mirrored": 8,
        }
    else:
        return None

    hook = getattr(cls, "__setstate__", None)
    if not callable(hook):
        return {"status": "failure", "reason": "missing-__setstate__"}
    restored = cls.__new__(cls)
    hook(restored, state)
    if key == "notebooklm.types.Notebook":
        observed = {
            "last_viewed_at_restored": restored.last_viewed_at,
            "chat_sessions_restored": restored.chat_sessions,
        }
    else:
        observed = {
            "fragment_start_char_restored": restored.fragment_start_char,
            "fragment_end_char_restored": restored.fragment_end_char,
            "answer_start_char_mirrored": restored.answer_start_char,
            "answer_end_char_mirrored": restored.answer_end_char,
        }
    return {
        "status": "success" if observed == expected else "mismatch",
        "invariants": {name: observed[name] == value for name, value in expected.items()},
        "current_after_legacy_restore": _pickle_contract(restored, identity=False),
    }


def _dataclass_contract(cls: type[Any]) -> dict[str, object]:
    params = cls.__dataclass_params__
    fields = dataclasses.fields(cls)
    signature = inspect.signature(cls)
    slots = cls.__dict__.get("__slots__", ())
    if isinstance(slots, str):
        slots = (slots,)
    has_slots = "__slots__" in cls.__dict__

    return {
        "kind": "dataclass",
        "module": cls.__module__,
        "qualname": cls.__qualname__,
        "dataclass_flags": {
            "eq": params.eq,
            "frozen": params.frozen,
            "init": params.init,
            "keyword_only": any(field.kw_only for field in fields),
            "match_args": "__match_args__" in cls.__dict__,
            "order": params.order,
            "repr": params.repr,
            "slots": has_slots,
            "unsafe_hash": params.unsafe_hash,
            "weakref_slot": has_slots and "__weakref__" in slots,
        },
        "slots": list(slots),
        "constructor_order": list(signature.parameters),
        "match_args": list(getattr(cls, "__match_args__", ())),
        "fields": [
            {
                "name": field.name,
                "type": _annotation_repr(field.type),
                "init": field.init,
                "repr": field.repr,
                "compare": field.compare,
                "hash": field.hash,
                "keyword_only": field.kw_only,
            }
            for field in fields
        ],
        "equality": _method_policy(cls, "__eq__"),
        "hash": _method_policy(cls, "__hash__"),
        "repr": _method_policy(cls, "__repr__"),
        "pickle_state_hooks": {
            "__getstate__": _state_hook_contract(cls, "__getstate__"),
            "__setstate__": _state_hook_contract(cls, "__setstate__"),
        },
        "pickle_round_trip": _pickle_contract(_valid_dataclass_sample(cls), identity=False),
        "legacy_state_round_trip": _legacy_state_contract(cls),
    }


def _enum_contract(cls: type[enum.Enum]) -> dict[str, object]:
    members = list(cls.__members__.items())
    sample = members[0][1]
    return {
        "kind": "enum",
        "module": cls.__module__,
        "qualname": cls.__qualname__,
        "members": [
            {
                "name": name,
                "value": member.value
                if isinstance(member.value, str | int | float | bool | type(None))
                else repr(member.value),
            }
            for name, member in members
        ],
        "equality": _method_policy(cls, "__eq__"),
        "hash": _method_policy(cls, "__hash__"),
        "repr": _method_policy(cls, "__repr__"),
        "pickle_state_hooks": {
            "__getstate__": _state_hook_contract(cls, "__getstate__"),
            "__setstate__": _state_hook_contract(cls, "__setstate__"),
        },
        "pickle_round_trip": _pickle_contract(sample, identity=True),
    }


def derive_public_model_contract() -> dict[str, object]:
    """Derive structural/pickle contracts for all exported models."""
    models: dict[str, object] = {}
    for cls, export_paths in sorted(
        _public_model_exports().items(), key=lambda item: _model_key(item[0])
    ):
        model_key = _model_key(cls)
        if model_key in models:
            raise ValueError(f"distinct public model identities collide at {model_key}")
        model = _enum_contract(cls) if issubclass(cls, enum.Enum) else _dataclass_contract(cls)
        model["exports"] = export_paths
        models[model_key] = model
    return {
        "schema_version": 1,
        "selection": (
            "every dataclass and enum in __all__ of every public module discovered by "
            "scripts/audit_public_api_compat.py, deduplicated by class identity"
        ),
        "models": models,
    }


def _field_type_contract(cls: type[Any]) -> list[dict[str, str]]:
    return [
        {"name": field.name, "type": _annotation_repr(field.type)}
        for field in dataclasses.fields(cls)
    ]


class _ContractError(RuntimeError):
    """Stable exception name for the telemetry error-path characterization."""


def _normalized_metrics_snapshot(snapshot: object) -> dict[str, object]:
    values = dataclasses.asdict(snapshot)
    for name, value in values.items():
        if isinstance(value, float) and value > 0.0:
            values[name] = "positive-float"
    return values


def _normalized_events(events: list[object]) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for event in events:
        projected.append(
            {
                "method": event.method,
                "status": event.status,
                "elapsed_seconds": "non-negative-float"
                if isinstance(event.elapsed_seconds, float) and event.elapsed_seconds >= 0.0
                else "invalid",
                "request_id": event.request_id,
                "error_type": event.error_type,
            }
        )
    return projected


async def _logical_rpc_scenario(
    outcome: str,
    *,
    disconnect_executor_metrics: bool = False,
) -> dict[str, object]:
    """Run one public ``rpc_call`` through production assembly and RpcExecutor."""
    from unittest.mock import AsyncMock, patch

    import httpx

    import notebooklm.rpc as rpc_module
    from notebooklm import correlation_id
    from notebooklm._client_metrics import ClientMetrics
    from notebooklm._runtime.rpc_call import RpcRequest, RpcResponse
    from notebooklm._runtime.transport import RuntimeTransport
    from notebooklm.auth import AuthTokens
    from notebooklm.client import NotebookLMClient
    from notebooklm.exceptions import DecodingError
    from notebooklm.rpc import RPCMethod
    from notebooklm.types import RpcTelemetryEvent
    from tests._fixtures.kernel_test_helpers import install_http_client_for_test

    events: list[RpcTelemetryEvent] = []

    def decode(
        _raw: str,
        rpc_id: str,
        *,
        allow_null: bool = False,
        raise_on_null_status: bool = False,
    ) -> dict[str, bool]:
        del allow_null, raise_on_null_status
        if outcome == "decode_error":
            raise DecodingError("contract decode drift", method_id=rpc_id)
        return {"ok": True}

    auth = AuthTokens(
        cookies={"SID": "contract-redacted"},
        csrf_token="contract-redacted",
        session_id="contract-redacted",
    )
    leaf_calls = 0

    async def fake_terminal(_transport: RuntimeTransport, request: RpcRequest) -> RpcResponse:
        nonlocal leaf_calls
        leaf_calls += 1
        await asyncio.sleep(0)
        if outcome == "transport_error":
            raise _ContractError("contract transport failure")
        return RpcResponse(
            response=httpx.Response(200, text=")]}'\n[]"),
            state=request.state,
        )

    with (
        patch.object(rpc_module, "decode_response", decode),
        patch.object(RuntimeTransport, "terminal", fake_terminal),
    ):
        client = NotebookLMClient(auth, on_rpc_event=events.append)
    install_http_client_for_test(
        client._backend._kernel,
        AsyncMock(spec=httpx.AsyncClient),
    )
    if disconnect_executor_metrics:
        client._backend._runtime._metrics = ClientMetrics(on_rpc_event=events.append)

    raised: str | None = None
    result: object = None
    with correlation_id("contract-request-id"):
        try:
            result = await client.rpc_call(RPCMethod.GET_NOTEBOOK, ["contract-notebook"])
        except (DecodingError, _ContractError) as exc:
            raised = type(exc).__qualname__

    return {
        "result": result,
        "raised": raised,
        "leaf_calls": leaf_calls,
        "events": _normalized_events(events),
        "metrics_snapshot": _normalized_metrics_snapshot(client.metrics_snapshot()),
    }


async def _logical_rpc_scenarios() -> dict[str, object]:
    return {
        "success": await _logical_rpc_scenario("success"),
        "transport_error": await _logical_rpc_scenario("transport_error"),
        "decode_error": await _logical_rpc_scenario("decode_error"),
    }


async def _supplemental_middleware_scenarios() -> dict[str, object]:
    import httpx

    from notebooklm._client_metrics import ClientMetrics
    from notebooklm._logging import reset_request_id, set_request_id
    from notebooklm._runtime.metrics_behavior import MetricsBehavior
    from notebooklm._runtime.rpc_call import RpcRequest, RpcResponse
    from notebooklm.types import RpcTelemetryEvent
    from tests._fixtures.chain import make_request

    async def run(*, rpc_method: str | None, failure: bool) -> dict[str, object]:
        events: list[RpcTelemetryEvent] = []

        async def capture(event: RpcTelemetryEvent) -> None:
            events.append(event)

        metrics = ClientMetrics(on_rpc_event=capture)
        middleware = MetricsBehavior(metrics)
        context = {} if rpc_method is None else {"rpc_method": rpc_method}
        request = make_request(
            url="https://contract.invalid",
            headers={},
            body=b"",
            context=context,
        )

        async def terminal(current: RpcRequest) -> RpcResponse:
            await asyncio.sleep(0)
            if failure:
                raise _ContractError("contract-error")
            return RpcResponse(httpx.Response(200), state=current.state)

        token = set_request_id("contract-request-id")
        try:
            try:
                await middleware(request, terminal)
            except _ContractError:
                if not failure:
                    raise
        finally:
            reset_request_id(token)

        return {
            "event_count": len(events),
            "events": _normalized_events(events),
            "snapshot": _normalized_metrics_snapshot(metrics.snapshot()),
        }

    return {
        "rpc_success": await run(rpc_method="CONTRACT_RPC", failure=False),
        "rpc_error": await run(rpc_method="CONTRACT_RPC", failure=True),
        "non_rpc_success": await run(rpc_method=None, failure=False),
        "non_rpc_error": await run(rpc_method=None, failure=True),
    }


def derive_metrics_contract() -> dict[str, object]:
    """Derive public metrics fields and location-independent emission semantics."""
    from notebooklm.types import ClientMetricsSnapshot, RpcTelemetryEvent

    async def derive_scenarios() -> tuple[dict[str, object], dict[str, object]]:
        return await _logical_rpc_scenarios(), await _supplemental_middleware_scenarios()

    logical, supplemental = asyncio.run(derive_scenarios())
    return {
        "schema_version": 1,
        "client_metrics_snapshot_fields": _field_type_contract(ClientMetricsSnapshot),
        "rpc_telemetry_event_fields": _field_type_contract(RpcTelemetryEvent),
        "logical_rpc_scenarios": logical,
        "supplemental_non_rpc_middleware_scenarios": supplemental,
    }


__all__ = [
    "_evidence_ast_fingerprint",
    "_secret_serialization_violations",
    "_validate_no_secret_channel_models",
    "derive_json_envelope_contract",
    "derive_metrics_contract",
    "derive_public_model_contract",
]
