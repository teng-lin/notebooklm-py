"""Detector coverage for the auth/browser shared-object mutation ratchet."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/audit_auth_shared_mutations.py"


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("audit_auth_shared_mutations", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _collect(audit, tmp_path: Path, body: str):
    source = tmp_path / "src/notebooklm/_auth"
    source.mkdir(parents=True)
    (source / "state.py").write_text(
        "class Owner:\n"
        "    _CACHE = {}\n"
        "    @classmethod\n"
        "    def process_default(cls): return cls\n"
        "REGISTRY = {'x': []}\n"
        "SHARED = Owner()\n"
        "def reset_default(): Owner.process_default().reset()\n",
        encoding="utf-8",
    )
    (source / "relay.py").write_text(
        "from . import state as nested\nfrom .state import Owner\n",
        encoding="utf-8",
    )
    (source.parent / "auth.py").write_text(
        "from ._auth import state as _state\n"
        "from ._auth.state import Owner\n"
        "SHARED_ALIAS = _state.SHARED\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_fake.py").write_text(body, encoding="utf-8")
    return audit.collect_mutations(tests, {"notebooklm._auth": source})


def test_classes_aliases_singletons_and_process_defaults_are_counted(audit, tmp_path):
    body = (
        "from notebooklm._auth import state as module\n"
        "from notebooklm._auth.state import Owner as Alias\n"
        "def helper(monkeypatch):\n"
        "    monkeypatch.setattr(Alias, '_flag', True)\n"
        "def test_x(monkeypatch):\n"
        "    module.SHARED.clear()\n"
        "    Alias.process_default().reset()\n"
        "    monkeypatch.setattr(module.Owner, '_other', True)\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert {(site.attribute, site.owner_kind) for site in sites} == {
        ("_flag", "helper"),
        ("clear", "test"),
        ("reset", "test"),
        ("_other", "test"),
    }


def test_module_gateways_nested_aliases_and_facade_reexports_are_counted(audit, tmp_path):
    body = (
        "from notebooklm import auth as facade\n"
        "from notebooklm._auth import state as module, relay\n"
        "from notebooklm._auth.state import reset_default as direct_reset\n"
        "def test_x():\n"
        "    module.reset_default()\n"
        "    relay.nested.reset_default()\n"
        "    direct_reset()\n"
        "    monkeypatch.setattr(facade.Owner, '_flag', True)\n"
        "    monkeypatch.setattr(relay.Owner, '_relay', True)\n"
        "    facade.SHARED_ALIAS.clear()\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert [(site.owner, site.attribute, site.idiom) for site in sites] == [
        ("notebooklm._auth.state.Owner", "_flag", "monkeypatch.setattr"),
        ("notebooklm._auth.state.Owner", "_relay", "monkeypatch.setattr"),
        (
            "notebooklm._auth.state.Owner.process_default()",
            "reset",
            "gateway-method-or-unknown",
        ),
        (
            "notebooklm._auth.state.Owner.process_default()",
            "reset",
            "gateway-method-or-unknown",
        ),
        (
            "notebooklm._auth.state.Owner.process_default()",
            "reset",
            "gateway-method-or-unknown",
        ),
        ("notebooklm._auth.state.SHARED", "clear", "mutator"),
    ]


def test_equivalent_process_default_accessor_and_shadowing(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def _owner():\n"
        "    return Owner.process_default()\n"
        "def test_x():\n"
        "    _owner().reset()\n"
        "def test_shadowed(_owner):\n"
        "    _owner().reset()\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert [(site.owner, site.attribute, site.owner_qualname) for site in sites] == [
        ("notebooklm._auth.state.Owner.process_default()", "reset", "test_x")
    ]


def test_direct_item_bulk_and_in_place_forms_are_counted(audit, tmp_path):
    body = (
        "from unittest.mock import patch\n"
        "from notebooklm._auth.state import Owner\n"
        "def test_x(monkeypatch):\n"
        "    Owner._CACHE['a'] = 1\n"
        "    Owner._CACHE.update({'b': 2})\n"
        "    patch.object(Owner, " + repr("_flag") + ", True)\n"
        "    patch.multiple(Owner, one=1, two=2)\n"
        "    patch.dict(vars(Owner), {'three': 3})\n"
        "    Owner.__dict__.update({'four': 4})\n"
        "    monkeypatch.delitem(Owner._CACHE, 'a')\n"
        "    del Owner._CACHE['b']\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert {site.idiom for site in sites} >= {
        "item-assignment",
        "mutator",
        "patch.object",
        "patch.multiple",
        "patch.dict",
        "namespace.update",
        "monkeypatch.delitem",
        "item-deletion",
    }


def test_item_idiom_does_not_leak_to_later_shared_assignment_targets(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def test_x():\n"
        "    Owner._CACHE['key'] = Owner._flag = 1\n"
    )

    sites = _collect(audit, tmp_path, body)

    assert [(site.attribute, site.idiom) for site in sites] == [
        ("_flag", "assignment"),
        ("key", "item-assignment"),
    ]


@pytest.mark.parametrize("copy_alias", [False, True], ids=["direct", "copied"])
@pytest.mark.parametrize("cross_package", [False, True], ids=["same-package", "cross-package"])
def test_ambiguous_shared_owner_aliases_fail_closed(
    audit, tmp_path, copy_alias: bool, cross_package: bool
):
    auth_source = tmp_path / "src/notebooklm/_auth"
    auth_source.mkdir(parents=True)
    (auth_source / "state.py").write_text(
        "class First:\n    _CACHE = {}\nclass Second:\n    _CACHE = {}\n",
        encoding="utf-8",
    )
    family = {"notebooklm._auth": auth_source}
    if cross_package:
        browser_source = tmp_path / "src/notebooklm/_browser"
        browser_source.mkdir()
        (browser_source / "state.py").write_text(
            "class BrowserOwner:\n    _CACHE = {}\n", encoding="utf-8"
        )
        family["notebooklm._browser"] = browser_source
        imports = (
            "from notebooklm._auth.state import First\n"
            "from notebooklm._browser.state import BrowserOwner\n"
        )
        alternate = "BrowserOwner"
    else:
        imports = "from notebooklm._auth.state import First, Second\n"
        alternate = "Second"
    copied = "    mutation_target = target\n" if copy_alias else ""
    target = "mutation_target" if copy_alias else "target"
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_fake.py").write_text(
        imports
        + "def test_x(monkeypatch, choose_alternate):\n"
        + "    if choose_alternate:\n"
        + f"        target = {alternate}\n"
        + "    else:\n"
        + "        target = First\n"
        + copied
        + f"    monkeypatch.setattr({target}, '_flag', True)\n",
        encoding="utf-8",
    )

    with pytest.raises(audit.AuditError, match="ambiguous family alias"):
        audit.collect_mutations(tests, family)


def test_finite_literal_loop_names_expand_for_shared_owners(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def test_x(monkeypatch):\n"
        "    for name in ('_one', '_two'):\n"
        "        monkeypatch.setattr(Owner, name, True)\n"
        "        Owner.__dict__[name] = True\n"
        "        patch.dict(vars(Owner), {name: True})\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert sorted((site.attribute, site.idiom) for site in sites) == sorted(
        [
            ("_one", "item-assignment"),
            ("_one", "monkeypatch.setattr"),
            ("_one", "patch.dict"),
            ("_two", "item-assignment"),
            ("_two", "monkeypatch.setattr"),
            ("_two", "patch.dict"),
        ]
    )


def test_helper_target_parameter_preserves_shared_owner_and_lexical_owner(audit, tmp_path):
    body = (
        "from notebooklm._auth import state\n"
        "def helper(mp, target):\n"
        "    mp.setattr(target, '_flag', True)\n"
        "def test_x(monkeypatch):\n"
        "    helper(monkeypatch, state.Owner)\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert [
        (site.owner, site.attribute, site.owner_qualname, site.owner_kind) for site in sites
    ] == [("notebooklm._auth.state.Owner", "_flag", "helper", "helper")]


def test_unresolved_shared_helper_target_parameter_fails_closed(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def helper(mp, target):\n"
        "    mp.setattr(target, '_flag', True)\n"
        "def test_x(monkeypatch, target):\n"
        "    helper(monkeypatch, Owner)\n"
        "    helper(monkeypatch, target)\n"
    )
    with pytest.raises(
        audit.AuditError, match="helper mutation target .* is not finitely resolved"
    ):
        _collect(audit, tmp_path, body)


def test_module_literal_constant_shadowed_by_shared_helper_parameter_fails_closed(audit, tmp_path):
    body = (
        "ATTR = '_flag'\n"
        "from notebooklm._auth.state import Owner\n"
        "def helper(monkeypatch, ATTR):\n"
        "    monkeypatch.setattr(Owner, ATTR, True)\n"
    )
    with pytest.raises(audit.AuditError, match="dynamic shared-owner attribute"):
        _collect(audit, tmp_path, body)


def test_fresh_nonfamily_shared_helper_target_is_excluded(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def helper(target):\n"
        "    target.clear()\n"
        "def test_x():\n"
        "    helper(Owner())\n"
    )
    assert _collect(audit, tmp_path, body) == []


def test_list_call_cannot_launder_shared_owner(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def helper(monkeypatch, targets):\n"
        "    monkeypatch.setattr(targets[0], '_flag', True)\n"
        "def test_x(monkeypatch):\n"
        "    helper(monkeypatch, list([Owner]))\n"
    )
    with pytest.raises(audit.AuditError, match="not finitely resolved"):
        _collect(audit, tmp_path, body)


def test_list_call_with_fresh_local_remains_excluded(audit, tmp_path):
    body = (
        "def helper(targets):\n"
        "    targets[0].append(1)\n"
        "def test_x():\n"
        "    local = []\n"
        "    helper(list([local]))\n"
    )
    assert _collect(audit, tmp_path, body) == []


def test_list_shared_value_stays_visible_through_local_forwarder(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def inner(monkeypatch, targets):\n"
        "    monkeypatch.setattr(targets[0], '_flag', True)\n"
        "def outer(monkeypatch, targets):\n"
        "    inner(monkeypatch, targets)\n"
        "def test_x(monkeypatch):\n"
        "    outer(monkeypatch, list([Owner]))\n"
    )
    with pytest.raises(audit.AuditError, match="not finitely resolved"):
        _collect(audit, tmp_path, body)


def test_nested_shared_containers_retain_stable_literal_key_identity(audit, tmp_path):
    body = (
        "from notebooklm._auth import state\n"
        "def test_x():\n"
        "    state.REGISTRY['x'].clear()\n"
        "    state.Owner._CACHE['x'].append(1)\n"
    )
    sites = _collect(audit, tmp_path, body)
    assert [(site.owner, site.attribute, site.idiom) for site in sites] == [
        ("notebooklm._auth.state.Owner._CACHE['x']", "append", "mutator"),
        ("notebooklm._auth.state.REGISTRY['x']", "clear", "mutator"),
    ]


def test_nested_shared_container_read_methods_are_excluded(audit, tmp_path):
    body = (
        "from notebooklm._auth import state\n"
        "def test_x():\n"
        "    state.REGISTRY['x'].isdisjoint({'other'})\n"
        "    state.REGISTRY['x'].issubset({'other'})\n"
    )
    assert _collect(audit, tmp_path, body) == []


def test_dynamic_nested_shared_container_key_fails_closed(audit, tmp_path):
    body = "from notebooklm._auth import state\ndef test_x(key):\n    state.REGISTRY[key].clear()\n"
    with pytest.raises(audit.AuditError, match="dynamic nested key"):
        _collect(audit, tmp_path, body)


def test_fresh_instances_and_shadowed_aliases_are_excluded(audit, tmp_path):
    body = (
        "from notebooklm._auth.state import Owner\n"
        "def test_x(monkeypatch):\n"
        "    fresh = Owner()\n"
        "    fresh.clear()\n"
        "    Owner = object()\n"
        "    monkeypatch.setattr(Owner, 'flag', True)\n"
    )
    assert _collect(audit, tmp_path, body) == []


@pytest.mark.parametrize(
    "statement",
    [
        "monkeypatch.setattr(Owner, name, True)",
        "patch.multiple(Owner, **values)",
        "patch.dict(vars(Owner), values)",
        "Owner._CACHE[name] = True",
    ],
)
def test_dynamic_shared_owner_mutations_fail_closed(audit, tmp_path, statement):
    body = (
        "from unittest.mock import patch\n"
        "from notebooklm._auth.state import Owner\n"
        "def test_x(monkeypatch, name, values):\n"
        f"    {statement}\n"
    )
    with pytest.raises(audit.AuditError, match="dynamic|unexpandable|literal"):
        _collect(audit, tmp_path, body)


def test_projection_retains_path_and_lexical_owner(audit, tmp_path):
    sites = _collect(
        audit,
        tmp_path,
        "from notebooklm._auth.state import Owner\n"
        "@pytest.fixture\n"
        "def reset(monkeypatch):\n"
        "    monkeypatch.setattr(Owner, '_flag', True)\n",
    )
    projection = audit.build_projection(sites)
    assert projection["version"] == 1
    assert projection["summary"] == {
        "total": 1,
        "private": 1,
        "helper_or_fixture": 1,
        "assignments": 0,
    }
    assert projection["mutations"][0]["owner_qualname"] == "reset"
    assert projection["mutations"][0]["owner_kind"] == "fixture"
