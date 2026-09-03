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
from pathlib import Path

import pytest

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
        ("tests/unit/test_warning_dedupe.py", "profile_store", "_STORAGE_LOCKS"),
        ("tests/unit/test_profile_atomic_write.py", "profile_store", "_STORAGE_LOCKS"),
        ("tests/unit/test_auth_account_coverage.py", "profile_store", "_STORAGE_LOCKS"),
    } <= actual
    account_commit_sites = [
        site
        for site in sites
        if site.path == "tests/unit/test_auth_profile_store_account.py"
        and site.module == "profile_store"
        and site.attribute == "_commit_profile_json"
    ]
    assert len(account_commit_sites) == 2


def test_live_replacement_patch_contract_and_scorecard_are_exact(script):
    sites = script.collect_sites(REPO_ROOT / "tests", REPO_ROOT / "src/notebooklm/_auth")
    projection = script.build_projection(sites)
    assert projection["summary"]["TOTAL"] == {
        "public": 99,
        "private": 144,
        "total": 243,
    }
    relevant = {
        (row["module"], row["attribute"], row["idiom"]): row["count"]
        for row in projection["sites"]
        if (row["module"], row["attribute"])
        in {
            ("account_email", "_write_account_metadata_if_document_unchanged"),
            ("profile_migration", "FileLock"),
            ("profile_migration", "atomic_write_json"),
            ("master_token", "MasterTokenFile"),
            ("master_token", "_bootstrapper"),
            ("master_token", "_verify_by_listing_notebooks"),
            ("master_token", "generate_android_id"),
            ("master_token_file", "_commit_master_token_json"),
            ("master_token_file", "_ensure_secure_parent_dir"),
            ("master_token_file", "_master_token_from_legacy_record"),
            ("master_token_file", "_master_token_to_legacy_record"),
            ("master_token_file", "_storage_state_lock_path"),
            ("profile_store", "MasterTokenFile"),
            ("storage", "MasterTokenFile"),
            ("storage", "write_master_token"),
            ("storage", "ProfileStore"),
            ("storage", "LegacyAccountMigrator"),
            ("storage", "replace_profile_from_login"),
            ("cookie_policy", "MINIMUM_REQUIRED_COOKIES"),
            ("cookie_policy", "cookie_names_from_storage"),
            ("profile_store", "_commit_profile_json"),
            ("profile_store", "filter_storage_state_cookies_by_domain_policy"),
            ("cookies", "build_httpx_cookies_from_storage"),
            ("cookies", "_build_httpx_cookies_from_storage_strict"),
            ("cookies", "_cookie_from_normalized_entry"),
            ("cookie_semantics", "sanitize_cookie_entry"),
            ("recovery", "_try_headless_reauth_result"),
            ("recovery", "_try_master_token_reauth_result"),
            ("recovery", "coalesced_cold_recovery"),
            ("recovery", "try_headless_reauth"),
            ("recovery", "try_master_token_reauth"),
            ("refresh", "_fetch_tokens_with_exact_baseline"),
            ("refresh", "_fetch_tokens_with_refresh"),
            ("storage", "save_cookies_to_storage"),
            ("storage", "get_account_email_for_storage"),
            ("tokens", "_load_stored_auth"),
            ("tokens", "resolve_auth_json_env"),
        }
    }
    assert relevant == {
        (
            "account_email",
            "_write_account_metadata_if_document_unchanged",
            "monkeypatch.setattr",
        ): 2,
        ("profile_migration", "FileLock", "monkeypatch.setattr"): 5,
        ("profile_migration", "atomic_write_json", "monkeypatch.setattr"): 2,
        ("master_token", "MasterTokenFile", "monkeypatch.setattr"): 4,
        ("master_token", "_bootstrapper", "monkeypatch.setattr"): 2,
        ("master_token", "_verify_by_listing_notebooks", "monkeypatch.setattr"): 1,
        ("master_token", "generate_android_id", "monkeypatch.setattr"): 1,
        ("master_token_file", "_commit_master_token_json", "monkeypatch.setattr"): 5,
        ("master_token_file", "_ensure_secure_parent_dir", "monkeypatch.setattr"): 3,
        (
            "master_token_file",
            "_master_token_from_legacy_record",
            "monkeypatch.setattr",
        ): 3,
        (
            "master_token_file",
            "_master_token_to_legacy_record",
            "monkeypatch.setattr",
        ): 2,
        ("master_token_file", "_storage_state_lock_path", "monkeypatch.setattr"): 2,
        ("profile_store", "MasterTokenFile", "monkeypatch.setattr"): 1,
        ("storage", "MasterTokenFile", "monkeypatch.setattr"): 1,
        ("storage", "write_master_token", "monkeypatch.setattr"): 1,
        ("storage", "ProfileStore", "monkeypatch.setattr"): 6,
        ("storage", "LegacyAccountMigrator", "monkeypatch.setattr"): 1,
        ("storage", "replace_profile_from_login", "monkeypatch.setattr"): 2,
        ("cookie_policy", "MINIMUM_REQUIRED_COOKIES", "monkeypatch.setattr"): 3,
        ("cookie_policy", "cookie_names_from_storage", "monkeypatch.setattr"): 1,
        ("profile_store", "_commit_profile_json", "monkeypatch.setattr"): 18,
        (
            "profile_store",
            "filter_storage_state_cookies_by_domain_policy",
            "monkeypatch.setattr",
        ): 7,
        ("cookies", "build_httpx_cookies_from_storage", "monkeypatch.setattr"): 3,
        ("cookies", "build_httpx_cookies_from_storage", "patch.object"): 4,
        ("cookies", "_build_httpx_cookies_from_storage_strict", "monkeypatch.setattr"): 1,
        ("cookies", "_cookie_from_normalized_entry", "monkeypatch.setattr"): 1,
        ("cookie_semantics", "sanitize_cookie_entry", "monkeypatch.setattr"): 1,
        ("recovery", "_try_headless_reauth_result", "monkeypatch.setattr"): 7,
        ("recovery", "_try_master_token_reauth_result", "monkeypatch.setattr"): 6,
        ("recovery", "coalesced_cold_recovery", "monkeypatch.setattr"): 2,
        ("refresh", "_fetch_tokens_with_exact_baseline", "monkeypatch.setattr"): 6,
        ("storage", "get_account_email_for_storage", "monkeypatch.setattr"): 1,
        ("tokens", "_load_stored_auth", "monkeypatch.setattr"): 6,
        ("tokens", "resolve_auth_json_env", "monkeypatch.setattr"): 1,
    }
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
    assert script.build_projection(sites) == {
        "version": 1,
        "summary": {
            "storage": {"public": 2, "private": 1, "total": 3},
            "TOTAL": {"public": 2, "private": 1, "total": 3},
        },
        "sites": [
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
        ],
    }


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
