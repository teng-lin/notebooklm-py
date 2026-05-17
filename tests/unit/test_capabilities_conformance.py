"""Structural conformance tests for the 9 base Protocols in ``_capabilities.py``.

These tests are pure structural checks: they assert that
``ClientCoreCapabilities`` (the adapter) — and, where applicable,
``ClientCore`` (the underlying concrete class) — expose every member
that each base Protocol declares. They never open the client, never
touch the network, and never instantiate via ``__init__``.

The ``__new__``-only ``ClientCore`` fixture is the explicit contract:
the test must run with zero runtime side-effects, so we bypass
``ClientCore.__init__`` entirely.

This test is intentionally noisy when a Protocol is added or removed —
the 9-Protocol count guard at the top forces future contributors to
update ``_BASE_PROTOCOLS`` in lockstep.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Protocol

import pytest

from notebooklm._capabilities import (
    AuthRouteProvider,
    ChatStreamingProvider,
    ClientCoreCapabilities,
    CookieJarProvider,
    CoreReqIdProvider,
    CoreRPCProvider,
    PollRegistryProvider,
    SourceListProvider,
    TransportOperationProvider,
    UploadConcurrencyProvider,
)
from notebooklm._core import ClientCore

_CAPABILITIES_SRC = Path(__file__).resolve().parents[2] / "src/notebooklm/_capabilities.py"

# Member names contributed by the ``typing.Protocol`` base itself. We filter
# these out when enumerating Protocol-declared members so the conformance
# checks only see the surface each *Provider* Protocol actually adds.
_PROTOCOL_BASE_DIR: frozenset[str] = frozenset(dir(Protocol))

# All 9 base Protocols, mirroring the bases of ``ClientCoreCapabilities``.
_BASE_PROTOCOLS: tuple[type, ...] = (
    CoreRPCProvider,
    SourceListProvider,
    CoreReqIdProvider,
    ChatStreamingProvider,
    PollRegistryProvider,
    AuthRouteProvider,
    CookieJarProvider,
    TransportOperationProvider,
    UploadConcurrencyProvider,
)

# Members of the 9 Protocols that ``ClientCore`` itself directly exposes today.
# The remaining members are deliberately adapter-only on main HEAD: they are
# either un-prefixed renames of underscored core helpers
# (e.g. ``begin_transport_post`` → ``ClientCore._begin_transport_post``),
# computed views (``authuser_query``/``authuser_header`` derive from
# ``self.auth`` plus formatting helpers), or instance-set values that a
# ``__new__``-only ``ClientCore`` cannot expose (``poll_registry`` is created
# in ``__init__``). Later decomposition (B2/C1c) may lift more of these onto
# ``ClientCore`` itself; when that happens, move them into this set.
_CORE_NATIVE_MEMBERS: frozenset[str] = frozenset(
    {
        "rpc_call",
        "get_source_ids",
        "next_reqid",
        "query_post",
        "get_upload_semaphore",
        "record_upload_queue_wait",
    }
)


def _protocol_members(protocol: type) -> list[str]:
    """Return public member names declared on ``protocol``.

    Combines :func:`inspect.getmembers`-style enumeration (filtered against
    the ``typing.Protocol`` base) with ``__annotations__`` so both
    method/property declarations and typed class-level attributes are
    captured. The Protocols in ``_capabilities.py`` use ``def`` /
    ``@property`` today, so ``__annotations__`` is empty — but the union
    keeps the recipe correct for any future annotation-style Protocol.
    """
    dir_members = {
        name
        for name in dir(protocol)
        if not name.startswith("_") and name not in _PROTOCOL_BASE_DIR
    }
    annotated = set(getattr(protocol, "__annotations__", {}).keys())
    return sorted(dir_members | annotated)


@pytest.fixture
def core() -> ClientCore:
    """Return a ``ClientCore`` constructed via ``__new__`` (no ``__init__``).

    Bypassing ``__init__`` is deliberate: the test asserts only structural
    conformance, never runtime behavior. This guarantees the test never
    opens an HTTP client, never starts a refresh loop, and never touches
    the network.
    """
    return ClientCore.__new__(ClientCore)


@pytest.fixture
def caps(core: ClientCore) -> ClientCoreCapabilities:
    """Return ``ClientCoreCapabilities`` wrapping the ``__new__``-only core."""
    return ClientCoreCapabilities(core)


def _ast_class_directly_inherits_protocol(node: ast.ClassDef) -> bool:
    """Return ``True`` iff ``node`` lists ``Protocol`` in its direct bases."""
    for base in node.bases:
        # Bare ``class Foo(Protocol):`` shows up as ``ast.Name(id="Protocol")``.
        if isinstance(base, ast.Name) and base.id == "Protocol":
            return True
        # Qualified ``class Foo(typing.Protocol):`` shows up as
        # ``ast.Attribute(value=ast.Name("typing"), attr="Protocol")``.
        if isinstance(base, ast.Attribute) and base.attr == "Protocol":
            return True
    return False


def test_capabilities_module_declares_exactly_nine_provider_protocols() -> None:
    """Guard against silent Protocol additions/removals.

    Scans the **top-level** AST of ``_capabilities.py`` for class
    definitions whose name ends with ``Provider`` AND which directly
    inherit from ``Protocol``. Both conditions matter: a stray non-Protocol
    ``FooProvider`` helper, or a Protocol renamed away from the ``Provider``
    suffix, would silently miscount. Future contributors who add a 10th
    Protocol must also extend ``_BASE_PROTOCOLS`` in this test so the
    conformance loops cover it; the assertions force that.
    """
    source = _CAPABILITIES_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    provider_classes = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name.endswith("Provider")
        and _ast_class_directly_inherits_protocol(node)
    ]
    assert len(provider_classes) == 9, (
        f"Expected exactly 9 *Provider Protocols in _capabilities.py, found "
        f"{len(provider_classes)}: {provider_classes}. Update _BASE_PROTOCOLS "
        "in this test if a Protocol was added or removed."
    )
    assert len(_BASE_PROTOCOLS) == 9, (
        "_BASE_PROTOCOLS drifted from the 9 *Provider classes in "
        "_capabilities.py; update the tuple."
    )
    # Defence-in-depth: the AST-discovered names must match the imported tuple.
    ast_names = set(provider_classes)
    runtime_names = {p.__name__ for p in _BASE_PROTOCOLS}
    assert ast_names == runtime_names, (
        f"AST-discovered Provider classes {ast_names} do not match "
        f"_BASE_PROTOCOLS imports {runtime_names}. Sync the tuple with the module."
    )


@pytest.mark.parametrize("protocol", _BASE_PROTOCOLS, ids=lambda p: p.__name__)
def test_client_core_capabilities_satisfies_protocol_structurally(
    protocol: type,
    caps: ClientCoreCapabilities,
) -> None:
    """``ClientCoreCapabilities`` concretely implements every Protocol member.

    Uses ``name in caps_cls.__dict__`` (concrete definition on the adapter
    itself) rather than ``hasattr(caps_cls, name)``. This matters because
    ``ClientCoreCapabilities`` inherits from all 9 Protocols, and ``typing.
    Protocol`` installs abstract stubs on its subclasses. A bare ``hasattr``
    would happily fall back to the inherited Protocol stub, masking a
    forgotten or accidentally-removed concrete implementation. The
    ``__dict__`` check forces an own-definition.

    Class-level inspection (not instance ``hasattr``) also avoids invoking
    ``@property`` descriptors against the ``__new__``-only core fixture.
    """
    members = _protocol_members(protocol)
    assert members, (
        f"{protocol.__name__} reported zero public members — Protocol enumeration is broken."
    )
    caps_cls = type(caps)
    missing = [name for name in members if name not in caps_cls.__dict__]
    assert not missing, (
        f"ClientCoreCapabilities does not concretely implement members for "
        f"{protocol.__name__}: {missing}. (A ``hasattr`` would silently fall "
        "back to the inherited Protocol stub; an own-definition is required.)"
    )

    # Honor the spec recipe: iterate ``__annotations__`` directly. These are
    # currently empty (Protocols declare members via ``def``/``@property``),
    # but the loop catches any future annotation-style Protocol additions.
    for name in getattr(protocol, "__annotations__", {}):
        assert name in caps_cls.__dict__, (
            f"ClientCoreCapabilities missing annotated member {protocol.__name__}.{name}"
        )


@pytest.mark.parametrize("protocol", _BASE_PROTOCOLS, ids=lambda p: p.__name__)
def test_client_core_exposes_native_protocol_surface(
    protocol: type,
    core: ClientCore,
) -> None:
    """``ClientCore`` directly exposes its native Protocol surface.

    Native members live on ``ClientCore`` itself and are forwarded
    transparently by the adapter. Non-native members are intentionally
    adapter-only on main HEAD (renames, computed views, or instance-set
    values that ``__new__`` cannot expose). When B2/C1c lift more of
    these onto ``ClientCore``, extend ``_CORE_NATIVE_MEMBERS`` accordingly.

    Uses class-level ``hasattr`` to avoid invoking instance state on the
    ``__new__``-only fixture.
    """
    core_cls = type(core)
    for name in _protocol_members(protocol):
        if name in _CORE_NATIVE_MEMBERS:
            assert hasattr(core_cls, name), (
                f"ClientCore lost its native {protocol.__name__}.{name} surface — "
                "did decomposition accidentally remove a direct method?"
            )

    # Honor the spec recipe: iterate ``__annotations__`` for the same
    # native-subset check. Empty today; future-proofing.
    for name in getattr(protocol, "__annotations__", {}):
        if name in _CORE_NATIVE_MEMBERS:
            assert hasattr(core_cls, name), (
                f"ClientCore missing annotated native member {protocol.__name__}.{name}"
            )


def test_client_core_capabilities_inherits_all_nine_protocols() -> None:
    """``ClientCoreCapabilities`` must declare every base Protocol in its MRO.

    Inheriting the Protocols is what makes the adapter usable as a typed
    handle for each Provider; without this, a feature API typed on (say)
    ``AuthRouteProvider`` could not accept a ``ClientCoreCapabilities``
    instance even if it had every method. The conformance check above
    confirms concrete implementations; this one confirms the type-level
    contract that callers depend on.
    """
    missing_bases = [p for p in _BASE_PROTOCOLS if p not in ClientCoreCapabilities.__mro__]
    assert not missing_bases, (
        f"ClientCoreCapabilities no longer inherits from: "
        f"{[p.__name__ for p in missing_bases]}. "
        "Restore the base class so callers typed on the Protocol can pass "
        "a ClientCoreCapabilities instance."
    )


def test_core_native_members_allowlist_is_symmetric(core: ClientCore) -> None:
    """``_CORE_NATIVE_MEMBERS`` must match the actual core-resident surface.

    Without this guard, the per-Protocol loops above are one-way: a member
    name dropped from the allowlist makes the relevant check skip silently,
    and a member renamed on ``ClientCore`` (so the old name no longer
    enumerates) similarly slips by. Comparing both sides catches drift in
    either direction, which is what makes the allowlist trustworthy.
    """
    core_cls = type(core)
    all_protocol_members: set[str] = set()
    for protocol in _BASE_PROTOCOLS:
        all_protocol_members.update(_protocol_members(protocol))

    actual_native = {name for name in all_protocol_members if hasattr(core_cls, name)}
    assert actual_native == _CORE_NATIVE_MEMBERS, (
        f"_CORE_NATIVE_MEMBERS is out of sync with ClientCore. "
        f"Members on ClientCore but not in allowlist: "
        f"{sorted(actual_native - _CORE_NATIVE_MEMBERS)}. "
        f"Members in allowlist but missing from ClientCore: "
        f"{sorted(_CORE_NATIVE_MEMBERS - actual_native)}."
    )


def test_protocol_member_enumeration_finds_known_members() -> None:
    """Smoke-test ``_protocol_members`` against a known Protocol.

    Without this guard, a future refactor that breaks Protocol member
    enumeration (e.g. by switching to ``__slots__`` or a metaclass that
    suppresses ``dir``) would silently turn the conformance tests above
    into vacuous no-ops.
    """
    assert _protocol_members(CoreRPCProvider) == ["rpc_call"]
    assert _protocol_members(AuthRouteProvider) == [
        "account_email",
        "authuser",
        "authuser_header",
        "authuser_query",
    ]
    assert _protocol_members(TransportOperationProvider) == [
        "begin_transport_post",
        "begin_transport_task",
        "finish_transport_post",
    ]


def test_core_fixture_skips_init(core: ClientCore) -> None:
    """The ``core`` fixture must NOT have run ``__init__``.

    ``__init__`` sets several instance attributes (e.g. ``poll_registry``);
    their absence on the fixture proves we bypassed initialization and
    thereby avoided opening an HTTP client or spawning tasks. Asserting
    ``vars(core)`` is empty is the strictest possible "no init ran" check
    and is robust against future refactors that move individual
    init-set attributes to lazy properties.
    """
    assert isinstance(core, ClientCore)
    assert vars(core) == {}, (
        f"ClientCore.__new__ fixture has instance attributes — __init__ ran? "
        f"vars(core)={vars(core)!r}"
    )


def test_inspect_get_members_returns_protocol_callables() -> None:
    """``inspect.getmembers`` discovers Protocol-declared callables/properties.

    Belt-and-braces check that the enumeration approach used by
    ``_protocol_members`` is consistent with ``inspect.getmembers``.
    """
    members = {
        name: obj
        for name, obj in inspect.getmembers(AuthRouteProvider)
        if not name.startswith("_") and name not in _PROTOCOL_BASE_DIR
    }
    assert set(members) == {
        "account_email",
        "authuser",
        "authuser_header",
        "authuser_query",
    }
