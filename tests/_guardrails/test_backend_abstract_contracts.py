"""Pin the exact abstract surface of each backend-neutral namespace base.

A shared workflow is allowed at most one wire hook. Namespace split commits
add an entry here in the same change that makes the public API class abstract,
so a new abstract read or hook is always an explicit review-visible diff.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.repo_lint


@dataclass(frozen=True)
class _AbstractContract:
    module: str
    class_name: str
    abstract_methods: frozenset[str]
    wire_hooks: frozenset[str]


# Empty in A0. A4-A9 append one contract per namespace split.
BASE_ABSTRACT_CONTRACTS: tuple[_AbstractContract, ...] = ()

_WIRE_HOOK_PREFIXES = ("_send_",)
_WIRE_HOOK_NAMES = frozenset({"_stream_answer"})


def test_backend_base_abstract_methods_and_wire_hooks_match_manifest() -> None:
    for contract in BASE_ABSTRACT_CONTRACTS:
        module = importlib.import_module(contract.module)
        base = getattr(module, contract.class_name)
        actual = frozenset(base.__abstractmethods__)
        actual_wire_hooks = frozenset(
            name
            for name in actual
            if name.startswith(_WIRE_HOOK_PREFIXES) or name in _WIRE_HOOK_NAMES
        )

        assert actual == contract.abstract_methods, (
            f"{contract.class_name} abstract surface changed: "
            f"expected {sorted(contract.abstract_methods)}, got {sorted(actual)}"
        )
        assert actual_wire_hooks == contract.wire_hooks, (
            f"{contract.class_name} wire hooks changed: "
            f"expected {sorted(contract.wire_hooks)}, got {sorted(actual_wire_hooks)}"
        )
