"""Detector tests for the static ``notebooklm._auth`` import-graph audit."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_auth_import_graph.py"


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location("audit_auth_import_graph", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _project(audit, tmp_path: Path, files: dict[str, str]):
    auth_dir = tmp_path / "src" / "notebooklm" / "_auth"
    auth_dir.mkdir(parents=True)
    for name, body in files.items():
        (auth_dir / f"{name}.py").write_text(body, encoding="utf-8")
    return audit.build_projection(auth_dir, repo_root=tmp_path)


def test_resolves_relative_absolute_aliased_and_package_imports(audit, tmp_path):
    result = _project(
        audit,
        tmp_path,
        {
            "__init__": "from . import alpha as renamed\n",
            "alpha": (
                "from .beta import VALUE as v\n"
                "from notebooklm._auth import gamma as g\n"
                "import notebooklm._auth.delta as d\n"
            ),
            "beta": "VALUE = 1\n",
            "gamma": "VALUE = 1\n",
            "delta": "VALUE = 1\n",
        },
    )
    assert result["edges"] == [
        {"source": "__init__", "target": "alpha", "scope": "module"},
        {"source": "alpha", "target": "beta", "scope": "module"},
        {"source": "alpha", "target": "delta", "scope": "module"},
        {"source": "alpha", "target": "gamma", "scope": "module"},
    ]
    assert result["modules"][0]["name"] == "__init__"


def test_parameterized_package_records_external_notebooklm_edges(audit, tmp_path):
    browser_dir = tmp_path / "src" / "notebooklm" / "_browser"
    browser_dir.mkdir(parents=True)
    (browser_dir / "capture.py").write_text(
        "from . import errors\n"
        "from .._auth import storage\n"
        "from .._auth.cookie_policy import app_host_scope_note\n"
        "from notebooklm import paths\n"
        "def lazy():\n    from notebooklm.exceptions import AuthError\n",
        encoding="utf-8",
    )
    (browser_dir / "errors.py").write_text("class Error(Exception):\n    pass\n", encoding="utf-8")

    result = audit.build_projection(
        browser_dir,
        repo_root=tmp_path,
        package_prefix="notebooklm._browser",
        include_external=True,
    )

    assert result["edges"] == [
        {"source": "capture", "target": "_auth.cookie_policy", "scope": "module"},
        {"source": "capture", "target": "_auth.storage", "scope": "module"},
        {"source": "capture", "target": "errors", "scope": "module"},
        {"source": "capture", "target": "exceptions", "scope": "function"},
        {"source": "capture", "target": "paths", "scope": "module"},
    ]


def test_scope_duplicate_self_and_scc_rules(audit, tmp_path):
    result = _project(
        audit,
        tmp_path,
        {
            "__init__": "",
            "a": (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    from . import b\n"
                "class C:\n    from . import b\n"
                "from . import a\n"
                "def outer():\n"
                "    from . import c\n"
                "    def inner():\n        from . import c\n"
            ),
            "b": "from . import a\n",
            "c": "def f():\n    from . import a\n",
        },
    )
    assert result["edges"] == [
        {"source": "a", "target": "b", "scope": "module"},
        {"source": "a", "target": "c", "scope": "function"},
        {"source": "b", "target": "a", "scope": "module"},
        {"source": "c", "target": "a", "scope": "function"},
    ]
    assert result["sccs"] == {
        "module_level": [["a", "b"]],
        "all_scopes": [["a", "b", "c"]],
    }


def test_live_projection_is_the_frozen_scorecard(audit):
    result = audit.build_projection()
    assert result["summary"] == {
        "modules": 33,
        "total_lines": 13964,
        "unique_edges": 127,
        "module_edges": 115,
        "function_local_edges": 12,
    }
    assert result["sccs"] == {
        "module_level": [],
        "all_scopes": [],
    }
    edges = {(edge["source"], edge["target"], edge["scope"]) for edge in result["edges"]}
    assert ("cookie_types", "cookies", "module") not in edges
    assert {
        ("cookie_types", "cookie_policy", "module"),
        ("cookie_types", "cookie_semantics", "module"),
        ("cookies", "cookie_types", "module"),
        ("cookies", "cookie_semantics", "module"),
    } <= edges
    assert {edge for edge in edges if "account_types" in edge[:2]} == {
        ("account", "account_types", "module"),
        ("account_repair", "account_types", "module"),
    }
    assert {edge for edge in edges if "account_repair" in edge[:2]} == {
        ("account", "account_repair", "module"),
        ("account_repair", "account_types", "module"),
        ("account_repair", "cookies", "function"),
        ("account_repair", "keepalive", "function"),
        ("account_repair", "profile_account", "module"),
        ("account_repair", "profile_migration", "module"),
        ("account_repair", "profile_store", "module"),
    }
    assert {edge for edge in edges if "profile_account" in edge[:2]} == {
        ("account_email", "profile_account", "module"),
        ("account_repair", "profile_account", "module"),
        ("profile_account", "cookie_types", "module"),
        ("profile_document", "profile_account", "module"),
        ("profile_migration", "profile_account", "module"),
        ("profile_store", "profile_account", "module"),
        ("storage", "profile_account", "module"),
        ("tokens", "profile_account", "module"),
    }
    assert {edge for edge in edges if "profile_document" in edge[:2]} == {
        ("account_email", "profile_document", "module"),
        ("cookie_merge", "profile_document", "module"),
        ("profile_migration", "profile_document", "module"),
        ("psidts_recovery", "profile_document", "module"),
        ("profile_document", "cookie_types", "module"),
        ("profile_document", "profile_account", "module"),
        ("profile_store", "profile_document", "module"),
        ("storage", "profile_document", "module"),
        ("tokens", "profile_document", "module"),
    }
    assert {edge for edge in edges if "cookie_merge" in edge[:2]} == {
        ("cookie_merge", "cookie_policy", "module"),
        ("cookie_merge", "cookie_semantics", "module"),
        ("cookie_merge", "cookie_types", "module"),
        ("cookie_merge", "profile_document", "module"),
        ("profile_store", "cookie_merge", "module"),
        ("psidts_recovery", "cookie_merge", "module"),
        ("storage", "cookie_merge", "module"),
    }
    assert {edge for edge in edges if "cookie_filter" in edge[:2]} == {
        ("cookie_filter", "cookie_policy", "module"),
        ("cookie_filter", "cookie_semantics", "module"),
        ("profile_store", "cookie_filter", "module"),
        ("storage", "cookie_filter", "module"),
    }
    assert ("storage", "cookie_policy", "module") not in edges
    assert ("storage", "cookie_semantics", "module") not in edges
    assert ("storage", "cookie_policy", "function") not in edges
    assert ("profile_store", "cookie_policy", "module") in edges
    assert {edge for edge in edges if "storage_lock" in edge[:2]} == {
        ("keepalive", "storage_lock", "module"),
        ("master_token_file", "storage_lock", "module"),
        ("profile_store", "storage_lock", "module"),
        ("storage", "storage_lock", "module"),
    }
    assert ("keepalive", "storage", "module") not in edges
    assert {edge for edge in edges if "credential_io" in edge[:2]} == {
        ("master_token_file", "credential_io", "module"),
        ("profile_store", "credential_io", "module"),
    }
    assert {edge for edge in edges if "master_token_file" in edge[:2]} == {
        ("master_token", "master_token_file", "module"),
        ("master_token_file", "credential_io", "module"),
        ("master_token_file", "master_token_types", "module"),
        ("master_token_file", "paths", "module"),
        ("master_token_file", "storage_lock", "module"),
        ("profile_store", "master_token_file", "module"),
        ("storage", "master_token_file", "module"),
    }
    assert {edge for edge in edges if "master_token_types" in edge[:2]} == {
        ("master_token", "master_token_types", "module"),
        ("master_token_bootstrap", "master_token_types", "module"),
        ("master_token_file", "master_token_types", "module"),
        ("mint_service", "master_token_types", "module"),
        ("profile_store", "master_token_types", "module"),
        ("storage", "master_token_types", "module"),
    }
    assert {edge for edge in edges if "mint_service" in edge[:2]} == {
        ("keepalive", "mint_service", "module"),
        ("master_token", "mint_service", "module"),
        ("master_token_bootstrap", "mint_service", "module"),
        ("mint_service", "master_token_types", "module"),
    }
    assert {edge for edge in edges if "profile_store" in edge[:2]} == {
        ("account_email", "profile_store", "module"),
        ("account_repair", "profile_store", "module"),
        ("master_token", "profile_store", "module"),
        ("master_token_bootstrap", "profile_store", "module"),
        ("profile_migration", "profile_store", "module"),
        ("refresh", "profile_store", "module"),
        ("profile_store", "cookie_filter", "module"),
        ("profile_store", "cookie_merge", "module"),
        ("profile_store", "cookie_policy", "module"),
        ("profile_store", "cookie_types", "module"),
        ("profile_store", "credential_io", "module"),
        ("profile_store", "master_token_file", "module"),
        ("profile_store", "master_token_types", "module"),
        ("profile_store", "paths", "module"),
        ("profile_store", "profile_account", "module"),
        ("profile_store", "profile_document", "module"),
        ("profile_store", "storage_lock", "module"),
        ("psidts_recovery", "profile_store", "module"),
        ("storage", "profile_store", "module"),
        ("tokens", "profile_store", "module"),
    }
    assert {edge for edge in edges if "master_token_bootstrap" in edge[:2]} == {
        ("master_token", "master_token_bootstrap", "module"),
        ("master_token_bootstrap", "cookie_semantics", "module"),
        ("master_token_bootstrap", "cookie_types", "module"),
        ("master_token_bootstrap", "master_token_types", "module"),
        ("master_token_bootstrap", "mint_service", "module"),
        ("master_token_bootstrap", "profile_store", "module"),
    }
    assert {edge for edge in edges if "profile_migration" in edge[:2]} == {
        ("account_email", "profile_migration", "module"),
        ("account_repair", "profile_migration", "module"),
        ("profile_migration", "cookies", "module"),
        ("profile_migration", "profile_account", "module"),
        ("profile_migration", "profile_document", "module"),
        ("profile_migration", "profile_store", "module"),
        ("recovery", "profile_migration", "function"),
        ("recovery", "profile_migration", "module"),
        ("storage", "profile_migration", "module"),
        ("tokens", "profile_migration", "module"),
    }
    assert {edge for edge in edges if "tokens" in edge[:2]} == {
        ("account_email", "tokens", "module"),
        ("session", "tokens", "module"),
        ("tokens", "account", "module"),
        ("tokens", "cookie_types", "module"),
        ("tokens", "cookies", "module"),
        ("tokens", "paths", "module"),
        ("tokens", "profile_account", "module"),
        ("tokens", "profile_document", "module"),
        ("tokens", "profile_migration", "module"),
        ("tokens", "profile_store", "module"),
        ("tokens", "psidts_recovery", "module"),
        ("tokens", "refresh", "module"),
        ("tokens", "storage", "module"),
    }
    assert {edge for edge in edges if "recovery" in edge[:2]} == {
        ("recovery", "cookie_types", "module"),
        ("recovery", "cookies", "function"),
        ("recovery", "cookies", "module"),
        ("recovery", "extraction", "function"),
        ("recovery", "extraction", "module"),
        ("recovery", "recovery_rungs", "function"),
        ("recovery", "master_token", "function"),
        ("recovery", "paths", "module"),
        ("recovery", "profile_migration", "function"),
        ("recovery", "profile_migration", "module"),
        ("recovery", "single_flight", "module"),
        ("recovery", "storage", "function"),
        ("recovery", "storage", "module"),
        ("refresh", "recovery", "module"),
        ("session", "recovery", "module"),
    }
    assert {edge for edge in edges if "recovery_rungs" in edge[:2]} == {
        ("recovery", "recovery_rungs", "function"),
    }
    assert {edge for edge in edges if "psidts_recovery" in edge[:2]} == {
        ("cookies", "psidts_recovery", "function"),
        ("psidts_recovery", "cookie_merge", "module"),
        ("psidts_recovery", "cookie_policy", "module"),
        ("psidts_recovery", "cookie_semantics", "module"),
        ("psidts_recovery", "cookie_types", "module"),
        ("psidts_recovery", "keepalive", "module"),
        ("psidts_recovery", "paths", "module"),
        ("psidts_recovery", "profile_document", "module"),
        ("psidts_recovery", "profile_store", "module"),
        ("tokens", "psidts_recovery", "module"),
    }
    assert ("storage", "master_token", "function") not in edges
    assert ("refresh", "cookie_types", "module") in edges
