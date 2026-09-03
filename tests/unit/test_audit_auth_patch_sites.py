"""Tests for ``scripts/audit_auth_patch_sites.py``.

The script is the source of the ADR-0033 patch-site metric quoted in review and
in PR descriptions, and it has already miscounted twice: once by resolving
aliases too loosely, and twice more under review on #2156: it read only
POSITIONAL arguments, so every keyword-form ``monkeypatch.setattr`` /
``patch.object`` went uncounted; and it credited a function-local that merely
SHADOWED a module alias, which over-counted by 44 sites. The first biases the
number down and the second up, and both are silent.

A detector nobody tests is a number nobody can trust, so these pin the shapes it
must see and the shapes it must not count.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests._baselines.registry import baseline_by_name

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_auth_patch_sites.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("audit_auth_patch_sites", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_module()


def _sites(script, tmp_path: Path, body: str, *, auth_module: str, module_body: str):
    """Run ``collect_sites`` over a one-file fake tests tree and fake ``_auth``."""
    auth_dir = tmp_path / "_auth"
    auth_dir.mkdir()
    (auth_dir / "__init__.py").write_text("", encoding="utf-8")
    (auth_dir / f"{auth_module}.py").write_text(module_body, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fake.py").write_text(body, encoding="utf-8")
    return script.collect_sites(tests_dir, auth_dir)


_MODULE_BODY = "SEAM = None\n_PRIVATE_SEAM = None\n"

# Assembled rather than written literally. A literal ``patch("notebooklm…")`` in
# this file is indistinguishable, to a source-scanning gate, from a real
# string-target patch — and it correctly trips both ADR-0007 guardrails
# (``test_no_forbidden_monkeypatches`` and ``test_string_patch_ratchet``). This is
# fixture TEXT the detector under test parses, not a patch this file performs, so
# the honest fix is to keep the pattern out of the source rather than allowlist a
# file that patches nothing.
_STRING_TARGET_FIXTURE = (
    "from unittest.mock import patch\n"
    "def test_x():\n"
    "    patch(" + repr("notebooklm._auth.storage.SEAM") + ")\n"
)


@pytest.mark.parametrize(
    ("label", "body", "expected"),
    [
        (
            "positional-setattr",
            "from notebooklm._auth import storage\n"
            "def test_x(monkeypatch):\n"
            "    monkeypatch.setattr(storage, 'SEAM', 1)\n",
            {("storage", "SEAM")},
        ),
        # The regression this file exists for: both idioms take their first two
        # arguments by keyword, and a positional-only scan silently drops them.
        (
            "keyword-setattr",
            "from notebooklm._auth import storage\n"
            "def test_x(monkeypatch):\n"
            "    monkeypatch.setattr(target=storage, name='SEAM', value=1)\n",
            {("storage", "SEAM")},
        ),
        (
            "keyword-patch-object",
            "from unittest.mock import patch\n"
            "from notebooklm._auth import storage\n"
            "def test_x():\n"
            "    patch.object(target=storage, attribute='SEAM')\n",
            {("storage", "SEAM")},
        ),
        (
            "mixed-positional-and-keyword",
            "from notebooklm._auth import storage\n"
            "def test_x(monkeypatch):\n"
            "    monkeypatch.setattr(storage, name='SEAM', value=1)\n",
            {("storage", "SEAM")},
        ),
        (
            "plain-assignment-rebinding",
            "from notebooklm._auth import storage\ndef test_x():\n    storage.SEAM = 1\n",
            {("storage", "SEAM")},
        ),
        (
            "new-attribute-assignment-cannot-launder",
            "from notebooklm._auth import storage\ndef test_x():\n    storage.NEW_SEAM = 1\n",
            {("storage", "NEW_SEAM")},
        ),
        (
            "annotated-assignment-rebinding",
            "from notebooklm._auth import storage\ndef test_x():\n    storage.SEAM: int = 1\n",
            {("storage", "SEAM")},
        ),
        (
            "aliased-module-import",
            "from notebooklm._auth import storage as _st\n"
            "def test_x(monkeypatch):\n"
            "    monkeypatch.setattr(_st, '_PRIVATE_SEAM', 1)\n",
            {("storage", "_PRIVATE_SEAM")},
        ),
        (
            "function-local-auth-import",
            "def test_x(monkeypatch):\n"
            "    from notebooklm._auth import storage as local_storage\n"
            "    monkeypatch.setattr(local_storage, 'SEAM', 1)\n",
            {("storage", "SEAM")},
        ),
    ],
)
def test_counted_shapes(script, tmp_path, label, body, expected):
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert {(s.module, s.attribute) for s in sites} == expected
    # Cardinality too: a set comparison collapses duplicates, so a collector that
    # reported one source site twice would inflate the metric and still pass.
    assert len(sites) == len(expected)


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # A BARE annotation rebinds nothing — ``storage.SEAM: int`` is a type
        # statement, not a patch. Counting it would inflate the metric.
        (
            "bare-annotation-no-value",
            "from notebooklm._auth import storage\ndef test_x():\n    storage.SEAM: int\n",
        ),
        # String-target patching is a separately-banned idiom, not this metric's
        # subject. See _STRING_TARGET_FIXTURE for why it is assembled.
        ("string-target", _STRING_TARGET_FIXTURE),
        # A local that merely SHADOWS a module alias is not a module patch. Note
        # the attribute is a REAL module-level name: an earlier version of this
        # fixture used an invented one, which passed for the wrong reason (the
        # module-level-name check rejected it) and so never exercised shadowing
        # at all. With a real name it is the genuine false positive, and it was
        # one until the scope check landed.
        (
            "shadowed-local-assignment",
            "from notebooklm._auth import storage\n"
            "def test_x():\n"
            "    storage = object()\n"
            "    storage.SEAM = 1\n",
        ),
        (
            "shadowed-local-setattr",
            "from notebooklm._auth import storage\n"
            "def test_x(monkeypatch):\n"
            "    storage = object()\n"
            "    monkeypatch.setattr(storage, 'SEAM', 1)\n",
        ),
        # A nested scope reads the ENCLOSING local, not the module.
        (
            "shadowed-in-enclosing-scope",
            "from notebooklm._auth import storage\n"
            "def test_x():\n"
            "    storage = object()\n"
            "    def inner():\n"
            "        storage.SEAM = 1\n"
            "    inner()\n",
        ),
        # A parameter shadows just as effectively as an assignment.
        (
            "shadowed-by-parameter",
            "from notebooklm._auth import storage\ndef test_x(storage):\n    storage.SEAM = 1\n",
        ),
        ("unrelated-module", "import os\ndef test_x():\n    os.environ = {}\n"),
    ],
)
def test_uncounted_shapes(script, tmp_path, label, body):
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert sites == [], f"{label} must not be counted, got {sites}"


def test_private_and_public_are_split(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(storage, 'SEAM', 1)\n"
        "    monkeypatch.setattr(storage, '_PRIVATE_SEAM', 1)\n"
    )
    summary = script.summarize(
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    )
    assert summary["storage"] == {"public": 1, "private": 1, "total": 2}
    # The whole row: a regression in aggregate public/private split would slip
    # past a bare total.
    assert summary["TOTAL"] == {"public": 1, "private": 1, "total": 2}


def test_parameterized_package_counts_browser_patch_sites(script, tmp_path):
    browser_dir = tmp_path / "_browser"
    browser_dir.mkdir()
    (browser_dir / "__init__.py").write_text("", encoding="utf-8")
    (browser_dir / "capture.py").write_text(_MODULE_BODY, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_browser.py").write_text(
        "from notebooklm._browser import capture\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(capture, 'SEAM', 1)\n",
        encoding="utf-8",
    )

    sites = script.collect_sites(
        tests_dir,
        browser_dir,
        package_dotted="notebooklm._browser",
    )

    assert [(site.module, site.attribute) for site in sites] == [("capture", "SEAM")]


def test_real_function_local_import_sites_are_not_dropped(script):
    sites = script.collect_sites(REPO_ROOT / "tests", REPO_ROOT / "src" / "notebooklm" / "_auth")
    actual = {(site.path, site.module, site.attribute) for site in sites}
    assert {
        (
            "tests/integration/concurrency/test_upload_timeout_config.py",
            "tokens",
            "_load_stored_auth",
        ),
        ("tests/unit/test_auth_refresh.py", "psidts_recovery", "_recover_psidts_inline"),
        ("tests/unit/test_runtime_lifecycle.py", "keepalive", "_rotate_cookies"),
    } <= actual
    account_commit_sites = [
        site
        for site in sites
        if site.path == "tests/unit/test_auth_profile_store_account.py"
        and site.module == "profile_store"
        and site.attribute == "_commit_profile_json"
    ]
    assert len(account_commit_sites) == 2


def test_cold_recovery_mint_patches_are_owned_by_tests():
    """The ten mint fakes stay lexical; no patching helper can gain consumers."""
    path = REPO_ROOT / "tests/unit/test_auth_cold_start_recovery.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    owners: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[str] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "patch"
                and node.func.attr == "object"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "MintService"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "mint"
            ):
                assert self.functions and self.functions[-1].startswith("test_")
                owners.append(f"tests/unit/test_auth_cold_start_recovery.py::{self.functions[-1]}")
            self.generic_visit(node)

    Visitor().visit(tree)
    assert Counter(owners) == Counter(
        {
            "tests/unit/test_auth_cold_start_recovery.py::test_auth_tokens_cold_start_remints_from_sibling_master_token": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_client_factory_reaches_cold_master_token_recovery": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_concurrent_cold_start_coalesces_one_master_token_mint": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_cancelled_waiter_does_not_cancel_shared_master_token_mint": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_cancelled_direct_l4_waiter_does_not_cancel_shared_mint": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_shared_l4_failure_fans_out_and_later_call_retries": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_cold_and_live_l4_recovery_share_one_master_token_mint": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_headless_retry_that_still_redirects_falls_through_to_l4": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_same_path_callers_keep_their_explicit_account_routes": 1,
            "tests/unit/test_auth_cold_start_recovery.py::test_mixed_headless_permissions_serialize_and_reuse_l4_success": 1,
        }
    )


def test_live_replacement_patch_contract_and_scorecard_are_exact(script):
    sites = script.collect_sites(REPO_ROOT / "tests", REPO_ROOT / "src/notebooklm/_auth")
    projection = script.build_projection(sites)
    committed = baseline_by_name("auth_patch_sites").load()
    assert projection["summary"] == committed["summary"]
    assert projection["version"] == 2
    assert (
        sum(row["count"] for row in projection["joint_sites"])
        == projection["summary"]["TOTAL"]["total"]
    )
    assert all(row["package"] == "notebooklm._auth" for row in projection["joint_sites"])
    grouped = {(row["module"], row["attribute"], row["idiom"]) for row in projection["sites"]}
    assert not any(
        row["module"] == "refresh" and row["attribute"] == "save_cookies_to_storage"
        for row in projection["sites"]
    )
    assert grouped.isdisjoint(
        {
            ("cookies", "get_storage_path", "monkeypatch.setattr"),
            ("master_token", "remint_from_stored_token", "patch.object"),
            ("master_token", "mint_cookies", "patch.object"),
            ("master_token", "persist_minted_jar", "patch.object"),
            ("psidts_recovery", "_load_storage_state", "monkeypatch.setattr"),
            ("storage", "clear_account_metadata", "patch.object"),
            ("storage", "write_account_metadata", "patch.object"),
        }
    )


def test_definition_headers_resolve_in_the_enclosing_scope(script, tmp_path):
    body = (
        "from unittest.mock import patch\n"
        "from notebooklm._auth import storage\n"
        "@patch.object(storage, 'SEAM')\n"
        "def decorated(\n"
        "    value: patch.object(storage, 'SEAM') = patch.object(storage, 'SEAM'),\n"
        ") -> patch.object(storage, 'SEAM'):\n"
        "    pass\n"
        "@patch.object(storage, 'SEAM')\n"
        "class HeaderClass(\n"
        "    patch.object(storage, 'SEAM'),\n"
        "    metaclass=patch.object(storage, 'SEAM'),\n"
        "):\n"
        "    pass\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert len(sites) == 7
    assert {(site.module, site.attribute, site.idiom) for site in sites} == {
        ("storage", "SEAM", "patch.object")
    }


def test_comprehension_scope_does_not_poison_outer_alias(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "[storage for storage in values]\n"
        "monkeypatch.setattr(storage, 'SEAM', 1)\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert [(site.module, site.attribute) for site in sites] == [("storage", "SEAM")]


@pytest.mark.parametrize(
    "body",
    [
        (
            "from notebooklm._auth import storage\n"
            "[monkeypatch.setattr(storage, 'SEAM', 1) for storage in values]\n"
        ),
        (
            "from notebooklm._auth import storage\n"
            "match value:\n"
            "    case storage:\n"
            "        monkeypatch.setattr(storage, 'SEAM', 1)\n"
        ),
        (
            "from notebooklm._auth import storage\n"
            "storage = object()\n"
            "monkeypatch.setattr(storage, 'SEAM', 1)\n"
        ),
    ],
    ids=("comprehension-capture", "match-capture", "later-rebinding"),
)
def test_sequential_captures_and_rebindings_are_not_false_sites(script, tmp_path, body):
    assert _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY) == []


def test_function_global_auth_import_resolves_inside_that_scope(script, tmp_path):
    body = (
        "def test_x(monkeypatch):\n"
        "    global storage\n"
        "    from notebooklm._auth import storage\n"
        "    monkeypatch.setattr(storage, 'SEAM', 1)\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert [(site.module, site.attribute) for site in sites] == [("storage", "SEAM")]


def test_projection_aggregates_idiom_counts_without_paths_or_lines(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(storage, 'SEAM', 1)\n"
        "    monkeypatch.setattr(storage, 'SEAM', 2)\n"
        "    storage._PRIVATE_SEAM = 3\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    projection = script.build_projection(sites)
    assert projection["version"] == 2
    assert projection["summary"] == {
        "storage": {"public": 2, "private": 1, "total": 3},
        "TOTAL": {"public": 2, "private": 1, "total": 3},
    }
    assert projection["sites"] == [
        {
            "module": "storage",
            "attribute": "SEAM",
            "idiom": "monkeypatch.setattr",
            "count": 2,
        },
        {
            "module": "storage",
            "attribute": "_PRIVATE_SEAM",
            "idiom": "assignment",
            "count": 1,
        },
    ]
    assert projection["files"] == [{"path": "tests/test_fake.py", "count": 3}]
    assert projection["owners"] == [
        {
            "path": "tests/test_fake.py",
            "owner_qualname": "test_x",
            "owner_kind": "test",
            "count": 3,
        }
    ]


def test_projection_bites_on_changed_idiom_and_count(script, tmp_path):
    monkeypatch_body = (
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(storage, 'SEAM', 1)\n"
    )
    assignment_body = "from notebooklm._auth import storage\ndef test_x():\n    storage.SEAM = 1\n"
    doubled_monkeypatch_body = (
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(storage, 'SEAM', 1)\n"
        "    monkeypatch.setattr(storage, 'SEAM', 2)\n"
    )
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "three").mkdir()
    first = script.build_projection(
        _sites(
            script,
            tmp_path / "one",
            monkeypatch_body,
            auth_module="storage",
            module_body=_MODULE_BODY,
        )
    )
    second = script.build_projection(
        _sites(
            script,
            tmp_path / "two",
            assignment_body,
            auth_module="storage",
            module_body=_MODULE_BODY,
        )
    )
    increased_count = script.build_projection(
        _sites(
            script,
            tmp_path / "three",
            doubled_monkeypatch_body,
            auth_module="storage",
            module_body=_MODULE_BODY,
        )
    )
    assert first != second
    assert first["sites"] == [
        {
            "module": "storage",
            "attribute": "SEAM",
            "idiom": "monkeypatch.setattr",
            "count": 1,
        }
    ]
    assert increased_count["sites"] == [
        {
            "module": "storage",
            "attribute": "SEAM",
            "idiom": "monkeypatch.setattr",
            "count": 2,
        }
    ]
    assert increased_count != first


def test_exact_module_facade_aliases_and_lexical_owner_are_recorded(script, tmp_path):
    facade = tmp_path / "auth.py"
    facade.write_text("SEAM = None\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_facade.py").write_text(
        "import notebooklm.auth as imported\n"
        "from notebooklm import auth as from_import\n"
        "def helper(monkeypatch):\n"
        "    monkeypatch.setattr(imported, 'SEAM', 1)\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(from_import, 'SEAM', 2)\n",
        encoding="utf-8",
    )
    sites = script.collect_sites(tests_dir, facade, package_dotted="notebooklm.auth")
    assert [(site.module, site.owner_qualname, site.owner_kind) for site in sites] == [
        ("auth", "helper", "helper"),
        ("auth", "test_x", "test"),
    ]


def test_helper_target_parameter_is_resolved_to_each_finite_call_target(script, tmp_path):
    auth_dir = tmp_path / "_auth"
    auth_dir.mkdir()
    (auth_dir / "__init__.py").write_text("", encoding="utf-8")
    for module in ("storage", "refresh"):
        (auth_dir / f"{module}.py").write_text(_MODULE_BODY, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fake.py").write_text(
        "from notebooklm._auth import refresh, storage\n"
        "def helper(mp, target):\n"
        "    mp.setattr(target, 'SEAM', 1)\n"
        "def relay(mp, target):\n"
        "    helper(mp, target)\n"
        "def test_x(monkeypatch):\n"
        "    relay(monkeypatch, storage)\n"
        "    relay(monkeypatch, refresh)\n",
        encoding="utf-8",
    )

    sites = script.collect_sites(tests_dir, auth_dir)

    assert [(site.module, site.attribute, site.owner_qualname) for site in sites] == [
        ("refresh", "SEAM", "helper"),
        ("storage", "SEAM", "helper"),
    ]
    assert {site.owner_kind for site in sites} == {"helper"}


def test_unresolved_helper_target_parameter_fails_closed(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def helper(mp, target):\n"
        "    mp.setattr(target, 'SEAM', 1)\n"
        "def test_x(monkeypatch, target):\n"
        "    helper(monkeypatch, storage)\n"
        "    helper(monkeypatch, target)\n"
    )
    with pytest.raises(
        script.AuditError, match="helper mutation target .* is not finitely resolved"
    ):
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)


def test_module_literal_constant_shadowed_by_helper_parameter_fails_closed(script, tmp_path):
    body = (
        "ATTR = 'SEAM'\n"
        "from notebooklm._auth import storage\n"
        "def helper(monkeypatch, ATTR):\n"
        "    monkeypatch.setattr(storage, ATTR, 1)\n"
    )
    with pytest.raises(script.AuditError, match="dynamic attribute"):
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)


def test_module_literal_constant_rebound_through_global_fails_closed(script, tmp_path):
    body = (
        "ATTR = 'SEAM'\n"
        "from notebooklm._auth import storage\n"
        "def helper(monkeypatch):\n"
        "    global ATTR\n"
        "    ATTR = input()\n"
        "    monkeypatch.setattr(storage, ATTR, 1)\n"
    )
    with pytest.raises(script.AuditError, match="dynamic attribute"):
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)


def test_fresh_nonfamily_helper_target_is_excluded(script, tmp_path):
    body = "def helper(target):\n    target.SEAM = 1\ndef test_x():\n    helper(object())\n"
    assert _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY) == []


def test_list_call_cannot_launder_family_module(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def helper(monkeypatch, targets):\n"
        "    monkeypatch.setattr(targets[0], 'SEAM', 1)\n"
        "def test_x(monkeypatch):\n"
        "    helper(monkeypatch, list([storage]))\n"
    )
    with pytest.raises(script.AuditError, match="not finitely resolved"):
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)


def test_list_call_with_fresh_local_remains_excluded(script, tmp_path):
    body = (
        "def helper(monkeypatch, targets):\n"
        "    monkeypatch.setattr(targets[0], 'SEAM', 1)\n"
        "def test_x(monkeypatch):\n"
        "    local = object()\n"
        "    helper(monkeypatch, list([local]))\n"
    )
    assert _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY) == []


def test_list_family_value_stays_visible_through_local_forwarder(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def inner(monkeypatch, targets):\n"
        "    monkeypatch.setattr(targets[0], 'SEAM', 1)\n"
        "def outer(monkeypatch, targets):\n"
        "    inner(monkeypatch, targets)\n"
        "def test_x(monkeypatch):\n"
        "    outer(monkeypatch, list([storage]))\n"
    )
    with pytest.raises(script.AuditError, match="not finitely resolved"):
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)


def test_bulk_and_namespace_mutations_expand_literal_keys(script, tmp_path):
    body = (
        "from unittest.mock import patch\n"
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    patch.multiple(storage, SEAM=1, _PRIVATE_SEAM=2)\n"
        "    patch.dict(vars(storage), {'SEAM': 3})\n"
        "    monkeypatch.setitem(storage.__dict__, '_PRIVATE_SEAM', 4)\n"
        "    storage.__dict__['SEAM'] = 5\n"
        "    del vars(storage)['_PRIVATE_SEAM']\n"
        "    vars(storage).update({'SEAM': 6}, _PRIVATE_SEAM=7)\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert sorted((site.attribute, site.idiom) for site in sites) == sorted(
        [
            ("SEAM", "item-assignment"),
            ("SEAM", "patch.dict"),
            ("SEAM", "patch.multiple"),
            ("_PRIVATE_SEAM", "item-deletion"),
            ("_PRIVATE_SEAM", "monkeypatch.setitem"),
            ("_PRIVATE_SEAM", "patch.multiple"),
            ("SEAM", "namespace.update"),
            ("_PRIVATE_SEAM", "namespace.update"),
        ]
    )


def test_item_idiom_does_not_leak_to_later_assignment_targets(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def test_x():\n"
        "    storage.__dict__['SEAM'] = storage._PRIVATE_SEAM = 1\n"
    )

    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)

    assert [(site.attribute, site.idiom) for site in sites] == [
        ("SEAM", "item-assignment"),
        ("_PRIVATE_SEAM", "assignment"),
    ]


def test_finite_literal_loop_names_expand_to_individual_rows(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    for name in ('SEAM', '_PRIVATE_SEAM'):\n"
        "        monkeypatch.setattr(storage, name, 1)\n"
        "        storage.__dict__[name] = 2\n"
        "        patch.dict(vars(storage), {name: 3})\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert sorted((site.attribute, site.idiom) for site in sites) == sorted(
        [
            ("SEAM", "item-assignment"),
            ("SEAM", "monkeypatch.setattr"),
            ("SEAM", "patch.dict"),
            ("_PRIVATE_SEAM", "item-assignment"),
            ("_PRIVATE_SEAM", "monkeypatch.setattr"),
            ("_PRIVATE_SEAM", "patch.dict"),
        ]
    )


def test_simple_lexical_module_alias_is_resolved(script, tmp_path):
    body = (
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch):\n"
        "    alias = storage\n"
        "    monkeypatch.setattr(alias, 'SEAM', 1)\n"
    )
    sites = _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)
    assert [(site.module, site.attribute) for site in sites] == [("storage", "SEAM")]


def _two_module_sites(script, tmp_path: Path, body: str):
    auth_dir = tmp_path / "_auth"
    auth_dir.mkdir()
    (auth_dir / "__init__.py").write_text("", encoding="utf-8")
    for module in ("refresh", "storage"):
        (auth_dir / f"{module}.py").write_text(_MODULE_BODY, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fake.py").write_text(body, encoding="utf-8")
    return script.collect_sites(tests_dir, auth_dir)


@pytest.mark.parametrize("copy_alias", [False, True], ids=["direct", "copied"])
def test_different_family_aliases_across_branches_fail_closed(script, tmp_path, copy_alias: bool):
    copied = "    mutation_target = target\n" if copy_alias else ""
    target = "mutation_target" if copy_alias else "target"
    body = (
        "from notebooklm._auth import refresh, storage\n"
        "def test_x(monkeypatch, choose_storage):\n"
        "    if choose_storage:\n"
        "        target = storage\n"
        "    else:\n"
        "        target = refresh\n"
        f"{copied}"
        f"    monkeypatch.setattr({target}, 'SEAM', 1)\n"
    )

    with pytest.raises(script.AuditError, match="ambiguous family alias"):
        _two_module_sites(script, tmp_path, body)


def test_same_family_alias_across_branches_remains_exact(script, tmp_path):
    body = (
        "from notebooklm._auth import refresh, storage\n"
        "def test_x(monkeypatch, choose_storage):\n"
        "    if choose_storage:\n"
        "        target = storage\n"
        "    else:\n"
        "        target = storage\n"
        "    monkeypatch.setattr(target, 'SEAM', 1)\n"
    )

    sites = _two_module_sites(script, tmp_path, body)

    assert [(site.module, site.attribute) for site in sites] == [("storage", "SEAM")]


def test_test_named_helper_in_noncollectable_file_stays_helper(script, tmp_path):
    auth_dir = tmp_path / "_auth"
    auth_dir.mkdir()
    (auth_dir / "__init__.py").write_text("", encoding="utf-8")
    (auth_dir / "storage.py").write_text(_MODULE_BODY, encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "helpers.py").write_text(
        "from notebooklm._auth import storage\n"
        "def test_named_helper(monkeypatch):\n"
        "    monkeypatch.setattr(storage, 'SEAM', 1)\n",
        encoding="utf-8",
    )
    sites = script.collect_sites(tests_dir, auth_dir)
    assert [(site.owner_qualname, site.owner_kind) for site in sites] == [
        ("test_named_helper", "helper")
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "monkeypatch.setattr(storage, name, 1)",
        "patch.multiple(storage, **values)",
        "patch.dict(storage.__dict__, values)",
        "monkeypatch.setitem(vars(storage), name, 1)",
        "storage.__dict__[name] = 1",
    ],
)
def test_dynamic_family_mutation_fails_closed(script, tmp_path, statement):
    body = (
        "from unittest.mock import patch\n"
        "from notebooklm._auth import storage\n"
        "def test_x(monkeypatch, name, values):\n"
        f"    {statement}\n"
    )
    with pytest.raises(script.AuditError, match="dynamic|unexpandable|literal"):
        _sites(script, tmp_path, body, auth_module="storage", module_body=_MODULE_BODY)


def test_missing_auth_dir_is_loud_not_a_silent_zero(script, tmp_path):
    """A renamed/missing ``_auth`` must not read as "the metric went down"."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fake.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        script.main(["--tests-dir", str(tests_dir), "--auth-dir", str(tmp_path / "nope")])
    # A bare ``raises(SystemExit)`` also accepts a CLEAN exit, which is exactly
    # the "silently counted nothing" outcome this guards against.
    assert exc_info.value.code not in (None, 0)


def test_script_parses_and_exposes_its_contract(script):
    """Guards the loader itself: the API these tests drive must exist."""
    for name in (
        "build_projection",
        "collect_sites",
        "summarize",
        "main",
        "load_module_level_names",
    ):
        assert hasattr(script, name), f"{name} disappeared from the audit script"
    ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
