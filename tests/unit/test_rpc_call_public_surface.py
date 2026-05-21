"""Public ``NotebookLMClient.rpc_call`` deprecation-warning surface tests.

These tests pin the v0.5.0 deprecation contract for the three kwargs
scheduled for removal in v0.6.0 (``source_path``, ``_is_retry``,
``operation_variant``):

* Default-shape calls (``client.rpc_call(method, params)``) MUST stay
  silent. The two ``simplefilter("error", DeprecationWarning)`` tests
  prove this: any stray ``DeprecationWarning`` re-raises as an exception
  and fails the test.
* Each deprecated kwarg has a single, exactly-worded warning message.
  Tests assert via ``str(w.message) == EXPECTED`` (NOT regex) so a typo
  in the implementation cannot slip past.
* ``stacklevel=2`` is verified by checking that the captured
  ``filename`` ends with this test file's name (the caller frame),
  not ``client.py`` (the implementation frame).
* Passing all three deprecated kwargs together emits three distinct
  warnings — the test asserts both ``len(...) == 3`` (catches
  duplicate-emission regressions) and the exact set of messages.

The narrow ``-W error::DeprecationWarning`` gate in this file's
verification run (see Phase 1 plan) errors on ANY DeprecationWarning
during these tests, so every test that expects a warning MUST capture
it via ``warnings.catch_warnings(record=True)`` + ``simplefilter("always")``.

See ``docs/deprecations.md`` for the canonical removal table.
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

_METHOD = RPCMethod.LIST_NOTEBOOKS
_EXPECTED_SOURCE_PATH_MSG = "rpc_call(source_path=...) is deprecated; removal v0.6.0"
_EXPECTED_IS_RETRY_MSG = "rpc_call(_is_retry=...) is deprecated; this is internal; removal v0.6.0"
_EXPECTED_OPERATION_VARIANT_MSG = "rpc_call(operation_variant=...) is deprecated; removal v0.6.0"


def _make_client() -> NotebookLMClient:
    """Build a NotebookLMClient with the core RPC patched to an AsyncMock.

    No real transport is initialized; ``client._session.rpc_call`` is
    replaced before any authed HTTP path can fire.
    """
    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    client._session.rpc_call = AsyncMock(return_value=[])
    return client


@pytest.mark.asyncio
async def test_default_call_emits_no_deprecation_warning() -> None:
    """A bare ``client.rpc_call(method, params)`` MUST be silent.

    ``simplefilter("error", DeprecationWarning)`` re-raises any
    DeprecationWarning as an exception, so this test fails loudly if
    the sentinel-default machinery ever warns on the default shape.
    The delegated call MUST receive today's literal defaults
    (``source_path="/"``, ``_is_retry=False``, ``operation_variant=None``).
    """
    client = _make_client()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        await client.rpc_call(_METHOD, [])
    client._session.rpc_call.assert_awaited_once_with(
        method=_METHOD,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
    )


@pytest.mark.asyncio
async def test_explicit_source_path_root_emits_no_warning() -> None:
    """Explicit ``source_path="/"`` matches the default → silent.

    Callers who are already passing the literal default value get a
    free pass; we only warn when the value actually differs from the
    pre-deprecation default behavior.
    """
    client = _make_client()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        await client.rpc_call(_METHOD, [], source_path="/")
    client._session.rpc_call.assert_awaited_once_with(
        method=_METHOD,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
    )


@pytest.mark.asyncio
async def test_non_root_source_path_emits_deprecation_warning() -> None:
    """Any ``source_path != "/"`` emits exactly one DeprecationWarning."""
    client = _make_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(_METHOD, [], source_path="/notebook/x")
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1
    assert str(dep[0].message) == _EXPECTED_SOURCE_PATH_MSG
    client._session.rpc_call.assert_awaited_once_with(
        method=_METHOD,
        params=[],
        source_path="/notebook/x",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
    )


@pytest.mark.asyncio
async def test_is_retry_explicit_false_emits_deprecation_warning() -> None:
    """``_is_retry=False`` still warns: any explicit value is a binding.

    Rationale: ``_is_retry`` is internal and will be removed in v0.6.0.
    Callers should not reach for it at all; even ``False`` (the
    historical default) is a sticky reference to the soon-to-disappear
    kwarg, so we warn them off.
    """
    client = _make_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(_METHOD, [], _is_retry=False)
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1
    assert str(dep[0].message) == _EXPECTED_IS_RETRY_MSG
    client._session.rpc_call.assert_awaited_once_with(
        method=_METHOD,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
    )


@pytest.mark.asyncio
async def test_is_retry_explicit_true_emits_deprecation_warning() -> None:
    """``_is_retry=True`` emits the same DeprecationWarning."""
    client = _make_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(_METHOD, [], _is_retry=True)
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1
    assert str(dep[0].message) == _EXPECTED_IS_RETRY_MSG
    client._session.rpc_call.assert_awaited_once_with(
        method=_METHOD,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=True,
        disable_internal_retries=False,
        operation_variant=None,
    )


@pytest.mark.asyncio
async def test_operation_variant_explicit_emits_deprecation_warning() -> None:
    """``operation_variant=<str>`` emits exactly one DeprecationWarning."""
    client = _make_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(_METHOD, [], operation_variant="x")
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1
    assert str(dep[0].message) == _EXPECTED_OPERATION_VARIANT_MSG
    client._session.rpc_call.assert_awaited_once_with(
        method=_METHOD,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant="x",
    )


@pytest.mark.asyncio
async def test_multiple_deprecated_kwargs_emit_multiple_warnings() -> None:
    """All three deprecated kwargs together → three distinct warnings.

    Asserts BOTH ``len(...) == 3`` (catches accidental
    duplicate-emission regressions) AND the exact set of messages
    (catches drift in any single warning text). The order of warnings
    is intentionally NOT asserted; ``warnings.simplefilter("always")``
    defeats Python's per-call dedupe so each branch fires once.
    """
    client = _make_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(
            _METHOD,
            [],
            source_path="/notebook/x",
            _is_retry=True,
            operation_variant="x",
        )
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 3
    dep_msgs = {str(w.message) for w in dep}
    assert dep_msgs == {
        _EXPECTED_SOURCE_PATH_MSG,
        _EXPECTED_IS_RETRY_MSG,
        _EXPECTED_OPERATION_VARIANT_MSG,
    }


@pytest.mark.asyncio
async def test_warning_stacklevel_is_two_so_caller_frame_is_user_code() -> None:
    """``stacklevel=2`` surfaces the caller frame, not ``client.py``.

    We capture the DeprecationWarning and check that ``filename`` ends
    with this test file's name (the caller). If ``stacklevel`` were 1,
    ``filename`` would end with ``client.py`` instead.
    """
    client = _make_client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client.rpc_call(_METHOD, [], source_path="/x")
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1
    assert dep[0].filename.endswith("test_rpc_call_public_surface.py"), (
        f"stacklevel=2 should surface caller frame; got {dep[0].filename}"
    )
