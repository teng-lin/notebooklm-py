"""Tests for ``scripts/audit_public_api_compat.py``."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import notebooklm.config as notebooklm_config

pytestmark = pytest.mark.repo_lint

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


def _signature(*params: dict, return_annotation: str | None = None) -> dict:
    payload = {"text": "(...)", "parameters": list(params)}
    if return_annotation is not None:
        payload["return_annotation"] = return_annotation
    return payload


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


def _constant(value_repr: str) -> dict:
    """A value-tracked module constant entry (VALUE_TRACKED_CONSTANTS)."""
    return {"kind": "str", "signature": None, "constant_value": value_repr}


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
        "collections.list",
        "mind_maps.generate",
        "mind_maps.get",
        "notebooks.list",
        "notes.create",
        "research.start",
        "settings.get_output_language",
        "sharing.set_public",
        "sources.add_url",
    } <= set(members)


def test_mind_maps_namespace_is_audited(script):
    assert "mind_maps" in script.CLIENT_NAMESPACE_ATTRIBUTES


def test_all_eleven_client_namespaces_are_audited(script):
    assert set(script.CLIENT_NAMESPACE_ATTRIBUTES) == {
        "artifacts",
        "chat",
        "collections",
        "labels",
        "mind_maps",
        "notes",
        "notebooks",
        "research",
        "settings",
        "sharing",
        "sources",
    }


def test_collect_manifest_captures_return_annotation(script):
    manifest = script.collect_manifest(REPO_ROOT)
    members = manifest["modules"]["notebooklm"]["exports"]["NotebookLMClient"]["members"]

    delete = members["sources.delete"]["signature"]
    assert "return_annotation" in delete
    assert delete["return_annotation"] == "None"

    # ``append_text`` is inherited from a postponed-annotations base while
    # ``delete`` is defined by a non-postponed Web facade. Both spell the same
    # public contract and must remain identical in the compatibility manifest.
    append_text = members["sources.append_text"]["signature"]
    assert append_text["return_annotation"] == "None"


def test_collect_manifest_canonicalizes_pep563_return_annotation(script):
    # ``_mind_maps_api`` uses ``from __future__ import annotations`` (PEP 563),
    # so ``mind_maps.get -> MindMap`` arrives as a bare string. The collector
    # must resolve it against the owning module's globals to the fully-qualified
    # form, otherwise a module flipping its PEP 563 status would surface a
    # spurious ``changed-return``.
    manifest = script.collect_manifest(REPO_ROOT)
    members = manifest["modules"]["notebooklm"]["exports"]["NotebookLMClient"]["members"]

    assert members["mind_maps.get"]["signature"]["return_annotation"] == "notebooklm.types.MindMap"


def test_collect_manifest_canonicalizes_neutral_enum_annotation_home(script):
    """The enum move keeps the historical audited compatibility spelling."""
    manifest = script.collect_manifest(REPO_ROOT)

    cases = (
        ("notebooklm", "Artifact", "report_format", "notebooklm.rpc.types.ReportFormat | None"),
        (
            "notebooklm",
            "NextStepSuggestion",
            "kind",
            "notebooklm.rpc.types.MagicArtifactType | None",
        ),
        (
            "notebooklm",
            "NotebookMetadata",
            "role",
            "notebooklm.rpc.types.SharePermission | None",
        ),
    )
    for module_name, class_name, member_name, expected in cases:
        member = manifest["modules"][module_name]["exports"][class_name]["members"][member_name]
        assert member["signature"]["return_annotation"] == expected


def test_collect_manifest_preserves_defaulted_dataclass_fields(script):
    manifest = script.collect_manifest(REPO_ROOT)
    members = manifest["modules"]["notebooklm"]["exports"]["GenerationStatus"]["members"]

    assert members["url"]["kind"] == "dataclass-field"


def test_signature_compare_allows_optional_parameter_addition(script):
    old = _signature(_param("notebook_id"))
    new = _signature(_param("notebook_id"), _param("timeout", default=True))

    assert script._signature_breakage(old, new) is None


def test_signature_compare_allows_newly_inspectable_signature(script):
    new = _signature(_param("notebook_id"))

    assert script._signature_breakage(None, new) is None


def test_signature_compare_rejects_newly_uninspectable_signature(script):
    old = _signature(_param("notebook_id"))

    assert script._signature_breakage(old, None) == f"signature changed from {old!r} to None"


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


def test_signature_compare_ignores_object_sentinel_default_address(script):
    # A bare ``object()`` sentinel default (e.g. wait_for_completion's
    # initial_interval) reprs as "<object object at 0xADDR>"; the hex address
    # differs between the baseline collector process and the current one, so
    # identical code must NOT read as a changed default (the v0.7.0 baseline
    # regression that this normalization fixes).
    old = _signature(
        _param("initial_interval", default=True, default_repr="<object object at 0x7f00aaaa>")
    )
    new = _signature(
        _param("initial_interval", default=True, default_repr="<object object at 0x55bbbbbb>")
    )

    assert script._signature_breakage(old, new) is None


def test_normalize_default_repr_strips_object_addresses(script):
    a = script.normalize_default_repr("<object object at 0x7f001234>")
    b = script.normalize_default_repr("<object object at 0x55009999>")
    assert a == b == "<object object at 0x...>"
    # a genuine default differs in more than the address and is preserved verbatim
    assert script.normalize_default_repr("5") == "5"
    assert script.normalize_default_repr(None) is None
    # ONLY the bare object() sentinel is normalized — an address-bearing instance
    # or function default is left intact, so a real change to it is still caught.
    assert script.normalize_default_repr("<Foo object at 0x7f00>") == "<Foo object at 0x7f00>"
    assert (
        script._signature_breakage(
            _signature(_param("cb", default=True, default_repr="<function f at 0x1>")),
            _signature(_param("cb", default=True, default_repr="<function g at 0x2>")),
        )
        == "default for parameter 'cb' changed from <function f at 0x1> to <function g at 0x2>"
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


def test_return_breakage_detects_changed_return_annotation(script):
    old = _signature(_param("self"), return_annotation="bool")
    new = _signature(_param("self"), return_annotation="None")

    assert script._return_breakage(old, new) == "return annotation changed from 'bool' to 'None'"


def test_return_breakage_ignores_unchanged_and_additive_annotations(script):
    same = _signature(_param("self"), return_annotation="None")
    assert script._return_breakage(same, same) is None

    # Older baselines predate return-annotation capture: a missing key on either
    # side, or an annotation appearing where there was none, is not a break.
    no_key = _signature(_param("self"))
    annotated = _signature(_param("self"), return_annotation="MindMap")
    assert script._return_breakage(no_key, annotated) is None
    assert script._return_breakage(annotated, no_key) is None
    # Key present with a null value: the function was unannotated at capture
    # time (distinct from the missing-key/old-baseline case above), so gaining
    # an annotation is still additive.
    none_to_value = {**no_key, "return_annotation": None}
    assert script._return_breakage(none_to_value, annotated) is None


def test_compare_manifests_flags_client_namespace_return_type_change(script):
    baseline = _manifest(
        {
            "NotebookLMClient": _class(
                members={
                    "mind_maps.get": {
                        "kind": "method",
                        "signature": _signature(
                            _param("self"),
                            _param("notebook_id"),
                            return_annotation="dict[str, Any] | None",
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
                    "mind_maps.get": {
                        "kind": "method",
                        "signature": _signature(
                            _param("self"),
                            _param("notebook_id"),
                            return_annotation="MindMap | None",
                        ),
                    },
                },
            )
        }
    )

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["changed-return"]
    assert breaks[0].object == "notebooklm.NotebookLMClient.mind_maps.get"
    assert "MindMap | None" in breaks[0].detail


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


def test_compare_manifests_detects_changed_constant_value(script):
    """A value-tracked constant rebound to a different value is a reviewable break.

    Before this, a public constant carried only its ``kind`` into the manifest, so
    repointing ``DEFAULT_BASE_URL`` at a different host compared as "str vs str" —
    identical — and the audit stayed green through the host flip.
    """
    baseline = _manifest({"DEFAULT_BASE_URL": _constant("'https://old.example'")})
    current = _manifest({"DEFAULT_BASE_URL": _constant("'https://new.example'")})

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["changed-constant-value"]
    assert breaks[0].object == "notebooklm.DEFAULT_BASE_URL"
    assert "old.example" in breaks[0].detail and "new.example" in breaks[0].detail


def test_compare_manifests_ignores_untracked_constant(script):
    """Only names in ``VALUE_TRACKED_CONSTANTS`` carry a fingerprint.

    Both sides lack ``constant_value``, so nothing is compared — adding or removing
    a name from the tracked set must not fire a break by itself.
    """
    baseline = _manifest({"SOME_CONSTANT": {"kind": "str"}})
    current = _manifest({"SOME_CONSTANT": {"kind": "str"}})

    assert script.compare_manifests(baseline, current) == []


def test_compare_manifests_ignores_one_sided_constant_fingerprint(script):
    """Newly tracking a constant is not itself a break.

    The baseline predates the name being tracked, so only one side has a
    fingerprint and there is nothing to compare against.
    """
    baseline = _manifest({"DEFAULT_BASE_URL": {"kind": "str"}})
    current = _manifest({"DEFAULT_BASE_URL": _constant("'https://new.example'")})

    assert script.compare_manifests(baseline, current) == []


def test_collect_manifest_captures_tracked_constant_values(script):
    """The tracked cookie-domain / host constants really do carry a fingerprint."""
    manifest = script.collect_manifest(REPO_ROOT)

    config_exports = manifest["modules"]["notebooklm.config"]["exports"]
    assert config_exports["DEFAULT_BASE_URL"]["constant_value"] == repr(
        notebooklm_config.DEFAULT_BASE_URL
    )

    auth_exports = manifest["modules"]["notebooklm.auth"]["exports"]
    required = auth_exports["REQUIRED_COOKIE_DOMAINS"]["constant_value"]
    assert ".google.com" in required
    # Untracked public exports stay fingerprint-free — the capture is opt-in.
    assert "constant_value" not in auth_exports["AuthTokens"]


def test_collect_manifest_constant_fingerprint_is_hash_seed_stable(script, monkeypatch):
    """Set/dict fingerprints must not depend on PYTHONHASHSEED.

    ``REQUIRED_COOKIE_DOMAINS`` is a frozenset, and each collection runs in a fresh
    subprocess. A raw ``repr()`` would order its members by hash and differ between
    the baseline run and the current run, reporting a break on every invocation
    while the value never changed.
    """

    def _tracked_constants(seed: str) -> dict[str, str]:
        monkeypatch.setenv("PYTHONHASHSEED", seed)
        exports = script.collect_manifest(REPO_ROOT)["modules"]["notebooklm.auth"]["exports"]
        return {
            name: exports[name]["constant_value"]
            for name in script.VALUE_TRACKED_CONSTANTS["notebooklm.auth"]
        }

    assert _tracked_constants("1") == _tracked_constants("2")


def _stable_value_repr(script):
    """Return the real ``stable_value_repr`` from the collector source.

    The function lives inside the ``_COLLECTOR`` script that the audit runs in a
    subprocess, so it is not an attribute of the loaded module. Lift the actual
    function definition out of that source rather than re-implementing it here —
    a copy would happily keep passing after the shipped one regressed.
    """
    tree = ast.parse(script._COLLECTOR)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "stable_value_repr"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<collector>", "exec"), namespace)
    return namespace["stable_value_repr"]


def test_stable_value_repr_is_order_insensitive_for_containers(script):
    """Set and dict members render sorted, so iteration order cannot leak in."""
    stable_value_repr = _stable_value_repr(script)

    assert stable_value_repr(frozenset({"b", "a"})) == stable_value_repr(frozenset({"a", "b"}))
    assert stable_value_repr({"b": 1, "a": 2}) == stable_value_repr({"a": 2, "b": 1})
    # Sequences keep their order — reordering a public tuple IS a change.
    assert stable_value_repr(("a", "b")) != stable_value_repr(("b", "a"))


def test_stable_value_repr_distinguishes_container_types(script):
    """A ``frozenset`` and a ``set`` with equal members must not fingerprint alike.

    Swapping the container of a published constant is a real contract change —
    a ``set`` lets callers mutate the library's own state — so an untagged
    ``{...}`` rendering (which also collides with an empty dict) would let it pass
    as no change.
    """
    stable_value_repr = _stable_value_repr(script)

    assert stable_value_repr(frozenset({"a"})) != stable_value_repr({"a"})
    assert stable_value_repr(frozenset()) != stable_value_repr({})
    # Nested, too: the members are equal, only the inner container type differs.
    assert stable_value_repr({"k": frozenset({"a"})}) != stable_value_repr({"k": {"a"}})


def test_compare_manifests_detects_nested_container_type_change(script):
    """The nested change above reaches ``compare_manifests`` as a break."""
    stable_value_repr = _stable_value_repr(script)
    baseline = _manifest({"TIERS": _constant(stable_value_repr({"k": frozenset({"a"})}))})
    current = _manifest({"TIERS": _constant(stable_value_repr({"k": {"a"}}))})

    breaks = script.compare_manifests(baseline, current)

    assert [item.code for item in breaks] == ["changed-constant-value"]
    assert breaks[0].object == "notebooklm.TIERS"


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


def test_stale_allowances_flags_entries_matching_no_break(script):
    # An allowance whose (code, object) matches a current break is live; one that
    # matches nothing is stale — it is already baked into the baseline.
    live_break = script.ApiBreak(
        code="removed-export",
        object="notebooklm.RealRemoval",
        detail="removed",
    )
    live = script.Allowance(
        code="removed-export",
        object="notebooklm.RealRemoval",
        reason="intentional removal pending next release",
    )
    stale = script.Allowance(
        code="removed-export",
        object="notebooklm.AlreadyInBaseline",
        reason="removed before the current baseline; matches nothing today",
    )

    result = script.stale_allowances([live_break], [live, stale])

    assert result == [stale]


def test_stale_allowances_returns_empty_when_every_entry_matches(script):
    brk = script.ApiBreak(code="changed-return", object="notebooklm.X.get", detail="narrowed")
    allowance = script.Allowance(
        code="changed-return", object="notebooklm.X.get", reason="documented narrowing"
    )

    assert script.stale_allowances([brk], [allowance]) == []


def test_stale_allowances_honors_glob_allowances(script):
    # A glob allowance that still covers a break is not stale.
    brk = script.ApiBreak(
        code="removed-member", object="notebooklm.Source.source_type", detail="removed"
    )
    glob = script.Allowance(
        code="removed-*", object="notebooklm.Source.*", reason="documented family removal"
    )

    assert script.stale_allowances([brk], [glob]) == []


def test_stale_allowances_treats_path_view_pair_as_one_unit(script):
    # Only the bare re-export view matches a break; the dotted client view does
    # not. The pair is still LIVE because either view matching keeps both — the
    # caveat the issue calls out (a single-view-detected break must not flag its
    # load-bearing sibling).
    bare_break = script.ApiBreak(
        code="changed-signature",
        object="notebooklm.NotebookLMClient.research.wait_for_completion",
        detail="removed interval=",
    )
    bare_view = script.Allowance(
        code="changed-signature",
        object="notebooklm.NotebookLMClient.research.wait_for_completion",
        reason="removed interval= alias",
    )
    client_view = script.Allowance(
        code="changed-signature",
        object="notebooklm.client.NotebookLMClient.research.wait_for_completion",
        reason="same break, dotted-module view",
    )

    assert script.stale_allowances([bare_break], [bare_view, client_view]) == []


def test_stale_allowances_flags_pair_when_neither_view_matches(script):
    # With no break at all, both views of a pair are stale and reported.
    bare_view = script.Allowance(
        code="removed-member", object="notebooklm.Old.gone", reason="stale"
    )
    client_view = script.Allowance(
        code="removed-member", object="notebooklm.client.Old.gone", reason="stale"
    )

    result = script.stale_allowances([], [bare_view, client_view])

    assert result == [bare_view, client_view]


def test_sibling_object_round_trips_the_two_path_views(script):
    bare = "notebooklm.NotebookLMClient.sources.get"
    client = "notebooklm.client.NotebookLMClient.sources.get"
    assert script._sibling_object(bare) == client
    assert script._sibling_object(client) == bare
    # An object outside the package prefix has no sibling.
    assert script._sibling_object("other.thing") is None


def test_audit_json_includes_stale_allowances_field(script, tmp_path, monkeypatch, capsys):
    # End-to-end shape check of ``--json`` / ``--check-stale`` without shelling
    # out to git: stub the manifest collection so a synthetic baseline removal
    # surfaces as one break, then confirm a non-matching allowance lands in
    # ``stale_allowances`` while a matching one lands in ``approved``.
    baseline = _manifest({"GoneExport": _function(), "KeptExport": _function()})
    current = _manifest({"KeptExport": _function()})

    monkeypatch.setattr(script, "latest_release_tag", lambda repo_root: "v9.9.9")
    monkeypatch.setattr(script, "export_git_ref", lambda repo_root, ref, dest: dest)

    def _stub_manifests():
        manifests = iter([baseline, current])
        monkeypatch.setattr(
            script,
            "collect_manifest",
            lambda root, extra=None, *, enforce_all=True: next(manifests),
        )

    _stub_manifests()

    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_breaks": [
                    {
                        "code": "removed-export",
                        "object": "notebooklm.GoneExport",
                        "reason": "intentional, pending next release",
                    },
                    {
                        "code": "removed-export",
                        "object": "notebooklm.AlreadyBaked",
                        "reason": "stale leftover",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = script.main(["--json", "--allowlist", str(allowlist)])
    payload = json.loads(capsys.readouterr().out)

    # ``--json`` without ``--check-stale`` reports stale entries but does not fail.
    assert exit_code == 0
    assert [item["object"] for item in payload["stale_allowances"]] == ["notebooklm.AlreadyBaked"]
    assert [item["break"]["object"] for item in payload["approved"]] == ["notebooklm.GoneExport"]

    # ``--check-stale`` promotes the same stale entry to a hard failure.
    _stub_manifests()
    assert script.main(["--check-stale", "--allowlist", str(allowlist)]) == 1

    # ``--prune`` performs the explicit write and reports the removed entry.
    _stub_manifests()
    assert script.main(["--prune", "--allowlist", str(allowlist)]) == 0
    assert json.loads(allowlist.read_text(encoding="utf-8"))["allowed_breaks"] == [
        {
            "code": "removed-export",
            "object": "notebooklm.GoneExport",
            "reason": "intentional, pending next release",
        }
    ]
    assert "Pruned stale allowlist entries" in capsys.readouterr().out


def test_stale_allowances_does_not_collide_on_same_object_different_codes(script):
    # Two allowances for the SAME object but different codes must be tracked
    # independently. Keying the match map by object alone would let the second
    # entry overwrite the first, wrongly flagging the live one as stale.
    brk = script.ApiBreak(code="removed-member", object="notebooklm.X", detail="removed")
    live = script.Allowance(
        code="removed-member", object="notebooklm.X", reason="intentional removal"
    )
    stale = script.Allowance(
        code="changed-signature", object="notebooklm.X", reason="describes no current break"
    )

    # Order-independent: the live entry stays live whether it is processed first
    # or last in the comprehension that builds the match map.
    assert script.stale_allowances([brk], [live, stale]) == [stale]
    assert script.stale_allowances([brk], [stale, live]) == [stale]


def test_prune_allowlist_preserves_policy_fields_and_removes_exact_entries(tmp_path, script):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "z_future_policy": {"enabled": True},
                "schema_version": 1,
                "extra_public_names": {"notebooklm": ["KeptName"]},
                "allowed_breaks": [
                    {
                        "code": "removed-export",
                        "object": "notebooklm.Stale",
                        "reason": "shipped",
                        "review_url": "https://example.invalid/review",
                    },
                    {
                        "code": "removed-export",
                        "object": "notebooklm.Live",
                        "reason": "still intentional",
                    },
                    {
                        "code": "removed-export",
                        "object": "notebooklm.Stale",
                        "reason": "different reason; keep",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    stale = [script.Allowance(code="removed-export", object="notebooklm.Stale", reason="shipped")]

    removed = script.prune_allowlist(policy, stale)

    assert removed == stale
    rewritten = json.loads(policy.read_text(encoding="utf-8"))
    assert rewritten["z_future_policy"] == {"enabled": True}
    assert rewritten["extra_public_names"] == {"notebooklm": ["KeptName"]}
    assert rewritten["allowed_breaks"] == [
        {
            "code": "removed-export",
            "object": "notebooklm.Live",
            "reason": "still intentional",
        },
        {
            "code": "removed-export",
            "object": "notebooklm.Stale",
            "reason": "different reason; keep",
        },
    ]
    assert policy.read_text(encoding="utf-8").endswith("\n")
    before = policy.stat().st_mtime_ns
    assert script.prune_allowlist(policy, stale) == []
    assert policy.stat().st_mtime_ns == before


def test_prune_allowlist_is_idempotent_and_pair_aware(tmp_path, script):
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_breaks": [
                    {
                        "code": "removed-member",
                        "object": "notebooklm.client.NotebookLMClient.sources.get",
                        "reason": "same pair",
                    },
                    {
                        "code": "removed-member",
                        "object": "notebooklm.NotebookLMClient.sources.get",
                        "reason": "same pair",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    pair_break = script.ApiBreak(
        code="removed-member",
        object="notebooklm.NotebookLMClient.sources.get",
        detail="still breaking",
    )
    stale = script.stale_allowances(
        [pair_break],
        script.load_policy(policy)[0],
    )

    assert stale == []
    before = policy.stat().st_mtime_ns
    assert script.prune_allowlist(policy, stale) == []
    assert policy.stat().st_mtime_ns == before


def test_prune_allowlist_reports_malformed_and_rewrite_errors(tmp_path, script, monkeypatch):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid JSON"):
        script.prune_allowlist(malformed, [])

    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_breaks": [
                    {"code": "removed-export", "object": "notebooklm.X", "reason": "old"}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        script.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("read-only filesystem"))
    )
    with pytest.raises(
        RuntimeError, match="could not atomically rewrite allowlist.*read-only filesystem"
    ):
        script.prune_allowlist(
            policy,
            [script.Allowance("removed-export", "notebooklm.X", "old")],
        )


def test_check_stale_does_not_print_ok_when_stale_blocks(script, tmp_path, monkeypatch, capsys):
    # When the compat surface is clean but --check-stale finds a stale entry, the
    # run exits 1 and must NOT print an "OK:" line that contradicts the failure.
    baseline = _manifest({"GoneExport": _function(), "KeptExport": _function()})
    current = _manifest({"KeptExport": _function()})

    monkeypatch.setattr(script, "latest_release_tag", lambda repo_root: "v9.9.9")
    monkeypatch.setattr(script, "export_git_ref", lambda repo_root, ref, dest: dest)
    manifests = iter([baseline, current])
    monkeypatch.setattr(
        script,
        "collect_manifest",
        lambda root, extra=None, *, enforce_all=True: next(manifests),
    )

    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_breaks": [
                    {
                        "code": "removed-export",
                        "object": "notebooklm.GoneExport",
                        "reason": "live: matches the synthetic removal",
                    },
                    {
                        "code": "removed-export",
                        "object": "notebooklm.AlreadyBaked",
                        "reason": "stale: matches nothing",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = script.main(["--check-stale", "--allowlist", str(allowlist)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "OK:" not in captured.out
    assert "OK:" not in captured.err
    assert "stale" in captured.err.lower()
    assert "notebooklm.AlreadyBaked" in captured.err


def test_latest_release_tag_skips_prereleases_and_nonrelease(script, tmp_path):
    """The default baseline stays on the last STABLE release tag.

    Pre-release (aN/bN/rcN) and non-release tags must not become the baseline,
    else pushing an alpha would silently rebaseline the compat gate.
    """
    import subprocess

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    git("init")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")

    (tmp_path / "f").write_text("1")
    git("add", "f")
    git("commit", "-m", "c1")
    git("tag", "v0.7.3")

    (tmp_path / "f").write_text("2")
    git("commit", "-am", "c2")
    git("tag", "docs-2026")  # stray non-release tag

    (tmp_path / "f").write_text("3")
    git("commit", "-am", "c3")
    git("tag", "v0.8.0a1")  # pre-release

    # Mid-cycle: baseline must skip BOTH the alpha and the stray tag.
    assert script.latest_release_tag(tmp_path) == "v0.7.3"

    (tmp_path / "f").write_text("4")
    git("commit", "-am", "c4")
    git("tag", "v0.8.0")  # final stable

    assert script.latest_release_tag(tmp_path) == "v0.8.0"
