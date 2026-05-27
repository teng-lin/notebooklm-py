"""Pin the ``__all__`` lists added in Tier 13 PR 13.9b.

The Session/Kernel split (Tier 13) renamed several private modules out of the
``_core_*`` namespace; PR 13.9b adds ``__all__`` to the collaborator modules
that lacked one so the surface they expose to first-party callers (and test
suites) is machine-checkable.

These pins guard against silent surface drift (e.g. someone exports a new
helper from a collaborator module without updating ``__all__`` or the
migration doc). They are NOT public-API contracts — see
``docs/stability.md`` for the public surface — but they pin the documented
internal surface listed in ``docs/refactor-history.md``.
"""

from __future__ import annotations

import notebooklm._conversation_cache as conversation_cache_module
import notebooklm._cookie_persistence as cookie_persistence_module
import notebooklm._request_types as request_types_module
import notebooklm._rpc_executor as rpc_executor_module
import notebooklm._streaming_post as streaming_post_module
import notebooklm._transport_errors as transport_errors_module

EXPECTED_REQUEST_TYPES_ALL: list[str] = [
    "AuthSnapshot",
    "BuildRequest",
    "BuildRequestResult",
    "PostBody",
    "materialize_build_request",
]

EXPECTED_STREAMING_POST_ALL: list[str] = [
    "MAX_RPC_RESPONSE_BYTES",
    "stream_post_with_size_cap",
]

EXPECTED_TRANSPORT_ERRORS_ALL: list[str] = [
    "MAX_RETRY_AFTER_SECONDS",
    "TransportAuthExpired",
    "TransportRateLimited",
    "TransportServerError",
    "parse_retry_after",
    "raise_mapped_post_error",
]

EXPECTED_RPC_EXECUTOR_ALL: list[str] = ["DecodeResponse", "RpcExecutor"]

EXPECTED_CONVERSATION_CACHE_ALL: list[str] = [
    "MAX_CONVERSATION_CACHE_SIZE",
    "ConversationCache",
]

EXPECTED_COOKIE_PERSISTENCE_ALL: list[str] = [
    "CookiePersistence",
    "SaveCookiesToStorage",
]


def _check_module_all(module: object, expected: list[str], label: str) -> None:
    actual = list(module.__all__)
    assert actual == expected, (
        f"{label}.__all__ drifted from the audited list.\n"
        f"missing: {sorted(set(expected) - set(actual))}\n"
        f"extra:   {sorted(set(actual) - set(expected))}\n"
        f"order:   actual={actual!r}\n"
        f"          expected={expected!r}"
    )
    # Every name in __all__ must resolve on the module.
    for name in expected:
        assert hasattr(module, name), (
            f"{label}.__all__ lists {name!r} but the module has no such attribute"
        )


def test_request_types_all_pinned() -> None:
    _check_module_all(
        request_types_module,
        EXPECTED_REQUEST_TYPES_ALL,
        "notebooklm._request_types",
    )


def test_streaming_post_all_pinned() -> None:
    _check_module_all(
        streaming_post_module,
        EXPECTED_STREAMING_POST_ALL,
        "notebooklm._streaming_post",
    )


def test_transport_errors_all_pinned() -> None:
    _check_module_all(
        transport_errors_module,
        EXPECTED_TRANSPORT_ERRORS_ALL,
        "notebooklm._transport_errors",
    )


def test_rpc_executor_all_pinned() -> None:
    _check_module_all(
        rpc_executor_module,
        EXPECTED_RPC_EXECUTOR_ALL,
        "notebooklm._rpc_executor",
    )


def test_conversation_cache_all_pinned() -> None:
    _check_module_all(
        conversation_cache_module,
        EXPECTED_CONVERSATION_CACHE_ALL,
        "notebooklm._conversation_cache",
    )


def test_cookie_persistence_all_pinned() -> None:
    _check_module_all(
        cookie_persistence_module,
        EXPECTED_COOKIE_PERSISTENCE_ALL,
        "notebooklm._cookie_persistence",
    )
