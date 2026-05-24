"""Tests for ``scripts/audit_public_api_compat.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_public_api_compat.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("audit_public_api_compat", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_module()


def _signature(*params: dict) -> dict:
    return {"text": "(...)", "parameters": list(params)}


def _param(
    name: str,
    *,
    default: bool = False,
    default_repr: str | None = None,
    kind: str = "POSITIONAL_OR_KEYWORD",
) -> dict:
    return {
        "name": name,
        "kind": kind,
        "has_default": default,
        "default_repr": default_repr,
    }


def _function(sig: dict | None = None) -> dict:
    return {"kind": "function", "signature": sig or _signature()}


def _class(*, members: dict | None = None, signature: dict | None = None) -> dict:
    return {
        "kind": "class",
        "signature": signature or _signature(),
        "members": members or {},
        "enum_members": {},
    }


def _manifest(exports: dict) -> dict:
    return {"modules": {"notebooklm": {"has_all": True, "exports": exports}}}


def test_compare_manifests_detects_removed_export(script):
    baseline = _manifest({"OldName": _function()})
    current = _manifest({})

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["removed-export"]
    assert breaks[0].object == "notebooklm.OldName"


def test_compare_manifests_detects_removed_module(script):
    baseline = {"modules": {"notebooklm.extra": {"has_all": True, "exports": {}}}}
    current = {"modules": {}}

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["removed-module"]
    assert breaks[0].object == "notebooklm.extra"


def test_compare_manifests_detects_removed_public_member(script):
    baseline = _manifest(
        {
            "Source": _class(
                members={"source_type": {"kind": "property", "signature": None}},
            )
        }
    )
    current = _manifest({"Source": _class(members={})})

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["removed-member"]
    assert breaks[0].object == "notebooklm.Source.source_type"


def test_compare_manifests_detects_removed_client_namespace_method(script):
    baseline = _manifest(
        {
            "NotebookLMClient": _class(
                members={
                    "sources": {"kind": "instance-attribute", "signature": None},
                    "sources.add_url": {"kind": "method", "signature": _signature()},
                },
            )
        }
    )
    current = _manifest(
        {
            "NotebookLMClient": _class(
                members={"sources": {"kind": "instance-attribute", "signature": None}},
            )
        }
    )

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["removed-member"]
    assert breaks[0].object == "notebooklm.NotebookLMClient.sources.add_url"


def test_compare_manifests_detects_client_namespace_method_signature_break(script):
    baseline = _manifest(
        {
            "NotebookLMClient": _class(
                members={
                    "sources.add_text": {
                        "kind": "method",
                        "signature": _signature(
                            _param("self"),
                            _param("notebook_id"),
                            _param("text"),
                            _param("title", default=True),
                        ),
                    },
                },
            )
        }
    )
    current = _manifest(
        {
            "NotebookLMClient": _class(
                members={
                    "sources.add_text": {
                        "kind": "method",
                        "signature": _signature(
                            _param("self"),
                            _param("notebook_id"),
                            _param("text"),
                        ),
                    },
                },
            )
        }
    )

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["changed-signature"]
    assert breaks[0].object == "notebooklm.NotebookLMClient.sources.add_text"
    assert "title" in breaks[0].detail


def test_collect_manifest_includes_representative_client_namespace_methods(script):
    manifest = script.collect_manifest(
        REPO_ROOT,
        {"notebooklm": ["configure_logging", "DEFAULT_STORAGE_PATH"]},
    )
    members = manifest["modules"]["notebooklm"]["exports"]["NotebookLMClient"]["members"]

    assert {
        "artifacts.download_audio",
        "chat.ask",
        "notebooks.list",
        "notes.create",
        "research.start",
        "settings.get_output_language",
        "sharing.set_public",
        "sources.add_url",
    } <= set(members)


def test_collect_manifest_preserves_defaulted_dataclass_fields(script):
    manifest = script.collect_manifest(REPO_ROOT)
    members = manifest["modules"]["notebooklm"]["exports"]["GenerationStatus"]["members"]

    assert members["url"]["kind"] == "dataclass-field"


def test_signature_compare_allows_optional_parameter_addition(script):
    old = _signature(_param("notebook_id"))
    new = _signature(_param("notebook_id"), _param("timeout", default=True))

    assert script._signature_breakage(old, new) is None


def test_signature_compare_rejects_required_parameter_addition(script):
    old = _signature(_param("notebook_id"))
    new = _signature(_param("notebook_id"), _param("timeout"))

    assert script._signature_breakage(old, new) == "new required parameter 'timeout' was added"


def test_signature_compare_rejects_removed_keyword_parameter(script):
    old = _signature(_param("notebook_id"), _param("source_path", default=True))
    new = _signature(_param("notebook_id"))

    assert script._signature_breakage(old, new) == "keyword parameter 'source_path' was removed"


def test_signature_compare_rejects_default_value_change(script):
    old = _signature(_param("wait", default=True, default_repr="False"))
    new = _signature(_param("wait", default=True, default_repr="True"))

    assert (
        script._signature_breakage(old, new)
        == "default for parameter 'wait' changed from False to True"
    )


def test_signature_compare_rejects_positional_parameter_reordering(script):
    old = _signature(_param("notebook_id"), _param("title"), _param("content"))
    new = _signature(_param("notebook_id"), _param("content"), _param("title"))

    assert (
        script._signature_breakage(old, new)
        == "positional parameter 'title' moved from position 2 to 3"
    )


def test_signature_compare_rejects_optional_positional_insertion_before_existing_slot(script):
    old = _signature(_param("notebook_id"), _param("content"))
    new = _signature(
        _param("notebook_id"),
        _param("encoding", default=True),
        _param("content"),
    )

    assert (
        script._signature_breakage(old, new)
        == "positional parameter 'content' moved from position 2 to 3"
    )


def test_signature_compare_rejects_removed_varargs(script):
    old = _signature(_param("args", kind="VAR_POSITIONAL"))
    new = _signature()

    assert (
        script._signature_breakage(old, new)
        == "old signature accepted *args, new signature does not"
    )


def test_signature_compare_rejects_removed_kwargs(script):
    old = _signature(_param("kwargs", kind="VAR_KEYWORD"))
    new = _signature()

    assert (
        script._signature_breakage(old, new)
        == "old signature accepted **kwargs, new signature does not"
    )


def test_compare_manifests_detects_enum_value_change(script):
    baseline = _manifest(
        {
            "SourceType": {
                "kind": "enum",
                "signature": _signature(),
                "members": {},
                "enum_members": {"PDF": "pdf"},
            }
        }
    )
    current = _manifest(
        {
            "SourceType": {
                "kind": "enum",
                "signature": _signature(),
                "members": {},
                "enum_members": {"PDF": "portable_document"},
            }
        }
    )

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["changed-enum-value"]
    assert breaks[0].object == "notebooklm.SourceType.PDF"


def test_compare_manifests_detects_removed_enum_member(script):
    baseline = _manifest(
        {
            "SourceType": {
                "kind": "enum",
                "signature": _signature(),
                "members": {},
                "enum_members": {"PDF": "pdf"},
            }
        }
    )
    current = _manifest(
        {
            "SourceType": {
                "kind": "enum",
                "signature": _signature(),
                "members": {},
                "enum_members": {},
            }
        }
    )

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["removed-enum-member"]
    assert breaks[0].object == "notebooklm.SourceType.PDF"


def test_allowance_partition_uses_code_and_object_globs(script):
    breakage = script.ApiBreak(
        code="removed-member",
        object="notebooklm.Source.source_type",
        detail="removed",
    )
    allowances = [
        script.Allowance(
            code="removed-*",
            object="notebooklm.Source.*",
            reason="documented deprecation removal",
        )
    ]

    unapproved, approved = script.partition_allowed([breakage], allowances)

    assert unapproved == []
    assert approved == [(breakage, allowances[0])]


def test_load_policy_reads_allowances_and_extra_public_names(tmp_path, script):
    policy = tmp_path / "policy.json"
    policy.write_text(
        """\
{
  "extra_public_names": {"notebooklm": ["DEFAULT_STORAGE_PATH"]},
  "allowed_breaks": [
    {
      "code": "removed-export",
      "object": "notebooklm.DEFAULT_STORAGE_PATH",
      "reason": "documented removal"
    }
  ]
}
""",
        encoding="utf-8",
    )

    allowances, extra_names = script.load_policy(policy)

    assert extra_names == {"notebooklm": ["DEFAULT_STORAGE_PATH"]}
    assert allowances == [
        script.Allowance(
            code="removed-export",
            object="notebooklm.DEFAULT_STORAGE_PATH",
            reason="documented removal",
        )
    ]


def test_load_policy_rejects_missing_allowlist(tmp_path, script):
    missing = tmp_path / "missing.json"

    with pytest.raises(RuntimeError, match="allowlist file not found"):
        script.load_policy(missing)


def test_load_policy_rejects_unsupported_schema_version(tmp_path, script):
    policy = tmp_path / "policy.json"
    policy.write_text(
        """\
{
  "schema_version": 2,
  "allowed_breaks": []
}
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsupported schema_version"):
        script.load_policy(policy)
