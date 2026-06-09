"""Audit public API compatibility against a previous release tag.

This is a release gate, not a replacement for unit tests. It compares the
runtime public surface in this checkout against a baseline git ref (by default
the latest reachable tag) and reports unapproved removals, call-signature
changes, or return-annotation changes.

Usage:
    uv run python scripts/audit_public_api_compat.py
    uv run python scripts/audit_public_api_compat.py --baseline-ref v0.4.1
    uv run python scripts/audit_public_api_compat.py --json
    uv run python scripts/audit_public_api_compat.py --check-stale

``--check-stale`` additionally fails when an ``allowed_breaks`` entry matches no
current break against the baseline (it is already in the baseline). This keeps
the allowlist from silently accumulating cruft — prune such entries at each
release boundary (see ``docs/releasing.md`` → prune-allowlist-at-release).

Exit codes:
    0  No unapproved compatibility breaks (and, with --check-stale, none stale).
    1  Unapproved public API breakage detected, or stale allowlist entries
       under --check-stale.
    2  Script/setup error, bad baseline ref, or import/introspection failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

PUBLIC_PACKAGE = "notebooklm"
DEFAULT_ALLOWLIST = "scripts/api-compat-allowlist.json"
EXCLUDED_TOP_LEVEL_MODULES = {"__main__", "notebooklm_cli"}
EXTRA_PUBLIC_PACKAGES = ("rpc",)
CLIENT_NAMESPACE_ATTRIBUTES = (
    "artifacts",
    "chat",
    "labels",
    "mind_maps",
    "notes",
    "notebooks",
    "research",
    "settings",
    "sharing",
    "sources",
)


@dataclass(frozen=True)
class ApiBreak:
    """A backward-incompatible public surface change."""

    code: str
    object: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.code}:{self.object}"


@dataclass(frozen=True)
class Allowance:
    """A reviewed compatibility break that is allowed for this release."""

    code: str
    object: str
    reason: str

    def matches(self, breakage: ApiBreak) -> bool:
        # These are fnmatch globs, so "*" can cross dots. Keep release
        # allowlists exact unless a broad match is intentional.
        return fnmatchcase(breakage.code, self.code) and fnmatchcase(
            breakage.object,
            self.object,
        )


def _run_git(args: list[str], cwd: Path, *, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=capture,
        text=False,
        check=False,
    )


def latest_release_tag(repo_root: Path) -> str:
    """Return the latest reachable version tag."""
    result = _run_git(["describe", "--tags", "--abbrev=0"], repo_root)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            "could not resolve latest tag: "
            f"{stderr.strip()}. Fetch tags/history or pass --baseline-ref explicitly."
        )
    return result.stdout.decode("utf-8").strip()


def export_git_ref(repo_root: Path, ref: str, destination: Path) -> Path:
    """Extract ``ref`` into ``destination`` and return the extracted repo path."""
    result = _run_git(["archive", "--format=tar", ref], repo_root)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"could not archive baseline ref {ref!r}: {stderr.strip()}")

    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / "baseline.tar"
    archive_path.write_bytes(result.stdout)
    source_root = destination / "baseline"
    source_root.mkdir()
    with tarfile.open(archive_path) as archive:
        archive.extractall(source_root, filter="data")
    return source_root


_COLLECTOR = r"""
from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import pathlib
import sys
import typing
import warnings

ROOT = pathlib.Path(sys.argv[1]).resolve()
EXTRA_PUBLIC_NAMES = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
CLIENT_NAMESPACE_ATTRIBUTES = set(json.loads(sys.argv[3])) if len(sys.argv) > 3 else set()
PKG = sys.argv[4]
EXCLUDED = set(json.loads(sys.argv[5]))
EXTRA_PACKAGES = tuple(json.loads(sys.argv[6]))
# Enforce the "every public module declares __all__" rule only for the CURRENT
# checkout. Historical baselines (e.g. v0.4.1) predate the rule and legitimately
# lack __all__ on some public modules; raising there would abort the baseline
# collection before any diff runs (issue #1493 review).
ENFORCE_ALL = (sys.argv[7] == "1") if len(sys.argv) > 7 else True


def discover_modules() -> list[str]:
    package_dir = ROOT / "src" / PKG
    modules = {PKG}
    if package_dir.is_dir():
        for path in package_dir.glob("*.py"):
            stem = path.stem
            if stem.startswith("_") or stem in EXCLUDED:
                continue
            modules.add(f"{PKG}.{stem}")
        for name in EXTRA_PACKAGES:
            if (package_dir / name / "__init__.py").is_file():
                modules.add(f"{PKG}.{name}")
    return sorted(modules)


class _ReturnProbe:
    # Tiny carrier so typing.get_type_hints can resolve a lone return string.
    def __init__(self, annotation):
        self.__annotations__ = {"return": annotation}


def annotation_repr(annotation, obj=None):
    if annotation is inspect.Signature.empty:
        return None
    # `from __future__ import annotations` (PEP 563) yields string annotations
    # already; non-postponed modules yield live objects. Resolve string
    # annotations against the owning module's globals so the captured form is
    # canonical regardless of a module's PEP 563 status (a transition would
    # otherwise flip e.g. 'MindMap' <-> 'notebooklm.types.MindMap' and surface a
    # spurious changed-return). Fall back to the raw string when resolution
    # fails (e.g. TYPE_CHECKING-only names).
    if isinstance(annotation, str):
        module_name = getattr(obj, "__module__", None)
        module = sys.modules.get(module_name) if module_name else None
        if module is None:
            return annotation
        try:
            annotation = typing.get_type_hints(
                _ReturnProbe(annotation), globalns=vars(module)
            )["return"]
        except Exception:
            return annotation
    return inspect.formatannotation(annotation)


def signature_payload(obj):
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    params = []
    for param in sig.parameters.values():
        params.append(
            {
                "name": param.name,
                "kind": param.kind.name,
                "has_default": param.default is not inspect.Parameter.empty,
                "default_repr": None
                if param.default is inspect.Parameter.empty
                else repr(param.default),
            }
        )
    return {
        "text": str(sig),
        "parameters": params,
        "return_annotation": annotation_repr(sig.return_annotation, obj),
    }


def kind_of(obj) -> str:
    if inspect.isclass(obj):
        if issubclass(obj, enum.Enum):
            return "enum"
        return "class"
    if inspect.isfunction(obj) or inspect.ismethod(obj) or inspect.iscoroutinefunction(obj):
        return "function"
    if inspect.ismodule(obj):
        return "module"
    return type(obj).__name__


def unwrap_member(obj):
    if isinstance(obj, (staticmethod, classmethod)):
        return obj.__func__
    if isinstance(obj, property):
        return obj.fget
    return obj


def member_kind(obj) -> str:
    if isinstance(obj, property):
        return "property"
    unwrapped = unwrap_member(obj)
    if inspect.isfunction(unwrapped) or inspect.ismethod(unwrapped):
        return "method"
    if inspect.isclass(unwrapped):
        return "class"
    return type(obj).__name__


def collect_class(cls) -> dict:
    payload = {
        "kind": kind_of(cls),
        "signature": signature_payload(cls),
        "members": {},
        "enum_members": {},
    }
    enum_member_names = set(cls.__members__) if issubclass(cls, enum.Enum) else set()
    if issubclass(cls, enum.Enum):
        payload["enum_members"] = {name: member.value for name, member in cls.__members__.items()}
    if dataclasses.is_dataclass(cls):
        for field in dataclasses.fields(cls):
            payload["members"][field.name] = {
                "kind": "dataclass-field",
                "signature": None,
            }
    for base in reversed(cls.__mro__):
        if base is object:
            continue
        if not getattr(base, "__module__", "").startswith(PKG):
            continue
        for name, raw in vars(base).items():
            if name.startswith("_"):
                continue
            if name in enum_member_names:
                continue
            if name in payload["members"]:
                continue
            target = unwrap_member(raw)
            payload["members"][name] = {
                "kind": member_kind(raw),
                "signature": signature_payload(target),
            }
    if cls.__module__ == f"{PKG}.client" and cls.__name__ == "NotebookLMClient":
        from notebooklm.auth import AuthTokens

        instance = cls(
            AuthTokens(
                cookies={"SID": "compat-audit"},
                csrf_token="compat-audit",
                session_id="compat-audit",
            )
        )
        for name in vars(instance):
            if not name.startswith("_"):
                payload["members"].setdefault(
                    name,
                    {
                        "kind": "instance-attribute",
                        "signature": None,
                    },
                )
                if name not in CLIENT_NAMESPACE_ATTRIBUTES:
                    continue
                subclient = getattr(instance, name)
                for base in reversed(type(subclient).__mro__):
                    if base is object:
                        continue
                    if not getattr(base, "__module__", "").startswith(PKG):
                        continue
                    for child_name, raw in vars(base).items():
                        if child_name.startswith("_"):
                            continue
                        target = unwrap_member(raw)
                        payload["members"][f"{name}.{child_name}"] = {
                            "kind": member_kind(raw),
                            "signature": signature_payload(target),
                        }
    return payload


def collect_module(module_name: str) -> dict:
    module = importlib.import_module(module_name)
    has_all = hasattr(module, "__all__")
    if not has_all and ENFORCE_ALL:
        # Every discovered public top-level module MUST declare ``__all__`` so a
        # brand-new public module cannot ship un-baselined (its surface would
        # otherwise be invisible to this audit). The presence flag was captured
        # historically but never enforced; enforce it now (issue #1493) — but
        # only for the current checkout (ENFORCE_ALL), never for an older
        # baseline that predates the rule.
        raise RuntimeError(
            f"public module {module_name!r} must declare __all__ "
            "(every public top-level module defines its exported surface so "
            "the compat audit can baseline it)"
        )
    all_names = list(getattr(module, "__all__", []))
    extra_names = list(EXTRA_PUBLIC_NAMES.get(module_name, []))
    names = []
    for name in [*all_names, *extra_names]:
        if name not in names:
            names.append(name)
    payload = {"exports": {}, "has_all": has_all}
    for name in names:
        try:
            value = getattr(module, name)
        except AttributeError:
            if name in extra_names and name not in all_names:
                continue
            raise
        entry = {
            "kind": kind_of(value),
            "signature": signature_payload(value)
            if (
                inspect.isfunction(value)
                or inspect.ismethod(value)
                or inspect.iscoroutinefunction(value)
            )
            else None,
        }
        if inspect.isclass(value):
            entry.update(collect_class(value))
        payload["exports"][name] = entry
    return payload


def main() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    warnings.simplefilter("ignore", DeprecationWarning)
    modules = discover_modules()
    manifest = {"modules": {}}
    errors = []
    for module_name in modules:
        try:
            manifest["modules"][module_name] = collect_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
    if errors:
        print(json.dumps({"errors": errors}, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(manifest, sort_keys=True))


main()
"""


_OBJECT_SENTINEL_REPR_RE = re.compile(r"<object object at 0x[0-9a-fA-F]+>")


def normalize_default_repr(default_repr: str | None) -> str | None:
    """Collapse a bare object() sentinel default repr to an address-free form.

    A bare object() sentinel default (e.g. the wait_for_completion
    initial_interval sentinel) reprs as <object object at 0xADDR>; the hex
    address differs between the baseline collector process and the current one,
    so identical code would otherwise read as a changed default. Only the bare
    object() sentinel (matching the whole repr) is normalized, so two
    same-identity sentinels compare equal while every other default — including
    an address-bearing instance or function repr — is left intact and a genuine
    change is still caught.
    """
    if default_repr is None:
        return None
    if _OBJECT_SENTINEL_REPR_RE.fullmatch(default_repr):
        return "<object object at 0x...>"
    return default_repr


def collect_manifest(
    source_root: Path,
    extra_public_names: dict[str, list[str]] | None = None,
    *,
    enforce_all: bool = True,
) -> dict[str, Any]:
    """Run the collector in a clean Python process for ``source_root``.

    ``enforce_all`` gates the "every public module declares ``__all__``" rule
    (issue #1493): pass ``True`` for the current checkout and ``False`` for a
    historical baseline that predates the rule, so baseline collection never
    aborts before the diff.
    """
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    pythonpath = str(source_root / "src")
    if existing_pythonpath:
        pythonpath = pythonpath + os.pathsep + existing_pythonpath
    env["PYTHONPATH"] = pythonpath

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _COLLECTOR,
            str(source_root),
            json.dumps(extra_public_names or {}, sort_keys=True),
            json.dumps(CLIENT_NAMESPACE_ATTRIBUTES),
            PUBLIC_PACKAGE,
            json.dumps(sorted(EXCLUDED_TOP_LEVEL_MODULES)),
            json.dumps(EXTRA_PUBLIC_PACKAGES),
            "1" if enforce_all else "0",
        ],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        if result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                pass
            else:
                errors = payload.get("errors") if isinstance(payload, dict) else None
                if isinstance(errors, list):
                    message = "; ".join(str(error) for error in errors)
        raise RuntimeError(f"public API collection failed for {source_root}: {message}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"public API collection returned invalid JSON for {source_root}: {exc}"
        ) from exc


def _has_kind(params: list[dict[str, Any]], kind: str) -> bool:
    return any(param["kind"] == kind for param in params)


def _accepts_keyword(param: dict[str, Any]) -> bool:
    return param["kind"] in {"POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY", "VAR_KEYWORD"}


def _accepts_positional(param: dict[str, Any]) -> bool:
    return param["kind"] in {
        "POSITIONAL_ONLY",
        "POSITIONAL_OR_KEYWORD",
        "VAR_POSITIONAL",
    }


def _signature_breakage(old: dict[str, Any] | None, new: dict[str, Any] | None) -> str | None:
    """Return a short incompatibility reason, or ``None`` when old calls still fit."""
    if old is None or new is None:
        if old != new:
            return f"signature changed from {old!r} to {new!r}"
        return None

    old_params = old["parameters"]
    new_params = new["parameters"]
    old_by_name = {param["name"]: param for param in old_params}
    new_by_name = {param["name"]: param for param in new_params}
    new_has_var_keyword = _has_kind(new_params, "VAR_KEYWORD")
    new_has_var_positional = _has_kind(new_params, "VAR_POSITIONAL")

    for old_param in old_params:
        kind = old_param["kind"]
        name = old_param["name"]
        if kind == "VAR_POSITIONAL" and not new_has_var_positional:
            return f"old signature accepted *{name}, new signature does not"
        if kind == "VAR_KEYWORD" and not new_has_var_keyword:
            return f"old signature accepted **{name}, new signature does not"
        if _accepts_keyword(old_param):
            new_param = new_by_name.get(name)
            if new_param is None:
                if not new_has_var_keyword:
                    return f"keyword parameter {name!r} was removed"
                continue
            if not _accepts_keyword(new_param):
                return f"parameter {name!r} no longer accepts keyword calls"
            if old_param["has_default"] and not new_param["has_default"]:
                return f"optional parameter {name!r} became required"
            old_default = normalize_default_repr(old_param.get("default_repr"))
            new_default = normalize_default_repr(new_param.get("default_repr"))
            if old_param["has_default"] and new_param["has_default"] and old_default != new_default:
                return f"default for parameter {name!r} changed from {old_default} to {new_default}"

    old_positional = [param for param in old_params if _accepts_positional(param)]
    new_positional = [param for param in new_params if _accepts_positional(param)]
    if not new_has_var_positional and len(new_positional) < len(old_positional):
        return (
            f"new signature accepts only {len(new_positional)} positional argument(s); "
            f"old accepted {len(old_positional)}"
        )

    old_fixed_positional = [param for param in old_positional if param["kind"] != "VAR_POSITIONAL"]
    new_fixed_positional = [param for param in new_positional if param["kind"] != "VAR_POSITIONAL"]
    new_fixed_names = [param["name"] for param in new_fixed_positional]
    for index, old_param in enumerate(old_fixed_positional):
        if index >= len(new_fixed_positional):
            break
        old_name = old_param["name"]
        new_name = new_fixed_positional[index]["name"]
        if old_name == new_name:
            continue
        if old_name in new_fixed_names:
            new_index = new_fixed_names.index(old_name)
            return (
                f"positional parameter {old_name!r} moved from position "
                f"{index + 1} to {new_index + 1}"
            )
        return (
            f"positional parameter {old_name!r} was replaced at position "
            f"{index + 1} by {new_name!r}"
        )

    for new_param in new_params:
        if new_param["kind"] in {"VAR_POSITIONAL", "VAR_KEYWORD"}:
            continue
        if new_param["has_default"]:
            continue
        if new_param["name"] not in old_by_name:
            return f"new required parameter {new_param['name']!r} was added"

    return None


def _return_breakage(old: dict[str, Any] | None, new: dict[str, Any] | None) -> str | None:
    """Return a reason when the return annotation changed, else ``None``.

    Older baselines predate return-annotation capture, so a missing
    ``return_annotation`` key is treated as "unknown" and never reported — only
    an observed value-to-value change counts as a break. An annotation appearing
    where there was none before is additive and also ignored. The mirror case —
    an annotation disappearing (annotated -> unannotated) — *is* reported;
    acknowledge it via the allowlist if intentional.
    """
    if old is None or new is None:
        return None
    if "return_annotation" not in old or "return_annotation" not in new:
        return None
    old_return = old["return_annotation"]
    new_return = new["return_annotation"]
    if old_return is None or old_return == new_return:
        return None
    return f"return annotation changed from {old_return!r} to {new_return!r}"


def _compare_export(
    module_name: str,
    export_name: str,
    old: dict[str, Any],
    new: dict[str, Any],
) -> list[ApiBreak]:
    path = f"{module_name}.{export_name}"
    breaks: list[ApiBreak] = []
    if old["kind"] != new["kind"]:
        breaks.append(
            ApiBreak(
                "changed-kind",
                path,
                f"kind changed from {old['kind']!r} to {new['kind']!r}",
            )
        )
        return breaks

    if old["kind"] in {"function", "class", "enum"}:
        reason = _signature_breakage(old.get("signature"), new.get("signature"))
        if reason:
            breaks.append(ApiBreak("changed-signature", path, reason))
        return_reason = _return_breakage(old.get("signature"), new.get("signature"))
        if return_reason:
            breaks.append(ApiBreak("changed-return", path, return_reason))

    for member_name, old_member in old.get("members", {}).items():
        new_member = new.get("members", {}).get(member_name)
        member_path = f"{path}.{member_name}"
        if new_member is None:
            breaks.append(
                ApiBreak(
                    "removed-member",
                    member_path,
                    f"{member_path} existed in the baseline and is missing now",
                )
            )
            continue
        if old_member["kind"] != new_member["kind"]:
            breaks.append(
                ApiBreak(
                    "changed-member-kind",
                    member_path,
                    f"kind changed from {old_member['kind']!r} to {new_member['kind']!r}",
                )
            )
            continue
        reason = _signature_breakage(old_member.get("signature"), new_member.get("signature"))
        if reason:
            breaks.append(ApiBreak("changed-signature", member_path, reason))
        return_reason = _return_breakage(old_member.get("signature"), new_member.get("signature"))
        if return_reason:
            breaks.append(ApiBreak("changed-return", member_path, return_reason))

    for enum_name, old_value in old.get("enum_members", {}).items():
        enum_members = new.get("enum_members", {})
        enum_path = f"{path}.{enum_name}"
        if enum_name not in enum_members:
            breaks.append(
                ApiBreak(
                    "removed-enum-member",
                    enum_path,
                    f"{enum_path} existed in the baseline and is missing now",
                )
            )
        elif enum_members[enum_name] != old_value:
            breaks.append(
                ApiBreak(
                    "changed-enum-value",
                    enum_path,
                    f"value changed from {old_value!r} to {enum_members[enum_name]!r}",
                )
            )

    return breaks


def compare_manifests(baseline: dict[str, Any], current: dict[str, Any]) -> list[ApiBreak]:
    """Return public API breaks from ``baseline`` to ``current``."""
    breaks: list[ApiBreak] = []
    current_modules = current.get("modules", {})
    for module_name, old_module in sorted(baseline.get("modules", {}).items()):
        new_module = current_modules.get(module_name)
        if new_module is None:
            breaks.append(
                ApiBreak(
                    "removed-module",
                    module_name,
                    f"public module {module_name} existed in the baseline and is missing now",
                )
            )
            continue
        for export_name, old_export in sorted(old_module.get("exports", {}).items()):
            new_export = new_module.get("exports", {}).get(export_name)
            export_path = f"{module_name}.{export_name}"
            if new_export is None:
                breaks.append(
                    ApiBreak(
                        "removed-export",
                        export_path,
                        f"{export_path} existed in the baseline public surface and is missing now",
                    )
                )
                continue
            breaks.extend(_compare_export(module_name, export_name, old_export, new_export))
    return sorted(breaks, key=lambda item: (item.code, item.object, item.detail))


def load_policy(path: Path | None) -> tuple[list[Allowance], dict[str, list[str]]]:
    if path is None or str(path) == "":
        return [], {}
    if not path.exists():
        raise RuntimeError(f"allowlist file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    schema_version = payload.get("schema_version", 1)
    if schema_version != 1:
        raise RuntimeError(f"{path}: unsupported schema_version {schema_version!r} (expected 1)")
    entries = payload.get("allowed_breaks", [])
    if not isinstance(entries, list):
        raise RuntimeError(f"{path} must contain an 'allowed_breaks' list")
    extra_public_names = payload.get("extra_public_names", {})
    if not isinstance(extra_public_names, dict):
        raise RuntimeError(f"{path} extra_public_names must be an object")
    normalized_extra_names: dict[str, list[str]] = {}
    for module_name, names in extra_public_names.items():
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise RuntimeError(
                f"{path} extra_public_names[{module_name!r}] must be a list of strings"
            )
        normalized_extra_names[str(module_name)] = sorted(set(names), key=str.lower)

    allowances: list[Allowance] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"{path}: allowed_breaks[{index}] must be an object")
        try:
            code = str(entry["code"])
            obj = str(entry["object"])
            reason = str(entry["reason"])
        except KeyError as exc:
            raise RuntimeError(
                f"{path}: allowed_breaks[{index}] is missing required key {exc.args[0]!r}"
            ) from exc
        allowances.append(Allowance(code=code, object=obj, reason=reason))
    return allowances, normalized_extra_names


def partition_allowed(
    breakages: list[ApiBreak],
    allowances: list[Allowance],
) -> tuple[list[ApiBreak], list[tuple[ApiBreak, Allowance]]]:
    unapproved: list[ApiBreak] = []
    approved: list[tuple[ApiBreak, Allowance]] = []
    for breakage in breakages:
        allowance = next((item for item in allowances if item.matches(breakage)), None)
        if allowance is None:
            unapproved.append(breakage)
        else:
            approved.append((breakage, allowance))
    return unapproved, approved


def _sibling_object(obj: str) -> str | None:
    """Return the other path-view of an exported object, or ``None``.

    The audit records the same client-namespace callable under two dotted
    paths: the re-export view ``notebooklm.X`` and the defining-module view
    ``notebooklm.client.X``. An allowance is written against one view; this maps
    between them so the staleness check can treat the pair as a single unit. A
    glob object (containing ``*``) is left for the caller to handle — the
    returned sibling is a literal string and is only consulted via an exact
    lookup, so a glob's sibling never spuriously matches.
    """
    client_prefix = f"{PUBLIC_PACKAGE}.client."
    bare_prefix = f"{PUBLIC_PACKAGE}."
    if obj.startswith(client_prefix):
        return bare_prefix + obj[len(client_prefix) :]
    if obj.startswith(bare_prefix):
        return client_prefix + obj[len(bare_prefix) :]
    return None


def stale_allowances(
    breakages: list[ApiBreak],
    allowances: list[Allowance],
) -> list[Allowance]:
    """Return allowances that match no current break against the baseline.

    An allowance is *stale* when it describes a break already baked into the
    baseline (so it no longer surfaces as a break against it). Such entries are
    dead weight: harmless to the gate, but the set only ever grows. Pruning them
    at each release boundary keeps the allowlist scoped to the breaks pending
    the *next* release (see ``docs/releasing.md`` → prune-allowlist-at-release).

    Pair-aware rule: the two path-views ``notebooklm.X`` and
    ``notebooklm.client.X`` of the same callable are treated as one unit — a
    unit is live (kept) if *either* view matches a break. So a non-stale
    allowance is one that itself matches a break, or whose sibling path-view has
    *any* matching allowance. Today both views always match together, but a
    future change that only one view detects must not flag its still load-bearing
    sibling.
    """
    # Per-allowance self-match, keyed by (code, object) so two allowances on the
    # same object but different codes never collapse onto one another.
    self_matched: dict[tuple[str, str], bool] = {
        (allowance.code, allowance.object): any(
            allowance.matches(breakage) for breakage in breakages
        )
        for allowance in allowances
    }
    # Per-object aggregate for the sibling lookup: an object is "kept" if *any*
    # of its allowances (any code) matches a break. The pair stays live as long
    # as the sibling object has a live allowance, regardless of code.
    object_kept: dict[str, bool] = {}
    for (_code, obj), is_match in self_matched.items():
        object_kept[obj] = object_kept.get(obj, False) or is_match

    def _is_live(allowance: Allowance) -> bool:
        if self_matched[(allowance.code, allowance.object)]:
            return True
        sibling = _sibling_object(allowance.object)
        return sibling is not None and object_kept.get(sibling, False)

    return [allowance for allowance in allowances if not _is_live(allowance)]


def _render_stale(stale: list[Allowance], baseline_ref: str, allowlist_path: Path | str) -> str:
    lines = [
        f"Stale allowlist entries — they match no break against the {baseline_ref} "
        "baseline, so they are already in the baseline:",
    ]
    for allowance in stale:
        lines.append(f"  - [{allowance.code}] {allowance.object}")
    lines.append(
        f"Prune them from {allowlist_path} (see docs/releasing.md → prune-allowlist-at-release)."
    )
    return "\n".join(lines)


def _render_breakages(title: str, breakages: list[ApiBreak]) -> str:
    if not breakages:
        return ""
    lines = [title]
    for breakage in breakages:
        lines.append(f"  - [{breakage.code}] {breakage.object}: {breakage.detail}")
    return "\n".join(lines)


def _render_approved(approved: list[tuple[ApiBreak, Allowance]]) -> str:
    if not approved:
        return ""
    lines = ["Allowlisted compatibility breaks:"]
    for breakage, allowance in approved:
        lines.append(f"  - [{breakage.code}] {breakage.object}: {allowance.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Compare the public Python API against a previous release tag."
    )
    parser.add_argument(
        "--baseline-ref",
        default=None,
        help="Git ref to compare against. Defaults to `git describe --tags --abbrev=0`.",
    )
    parser.add_argument(
        "--allowlist",
        default=str(repo_root / DEFAULT_ALLOWLIST),
        help=(
            "JSON file containing reviewed compatibility breaks. Use an empty string to disable."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    parser.add_argument(
        "--check-stale",
        action="store_true",
        help=(
            "Also fail when an allowlist entry matches no break against the "
            "baseline (it is already in the baseline — prune it)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        baseline_ref = args.baseline_ref or latest_release_tag(repo_root)
        allowlist_path = Path(args.allowlist) if args.allowlist else None
        allowances, extra_public_names = load_policy(allowlist_path)
        with tempfile.TemporaryDirectory(prefix="notebooklm-api-compat-") as tmp:
            baseline_root = export_git_ref(repo_root, baseline_ref, Path(tmp))
            # Enforce the __all__ rule only for the current checkout; an older
            # baseline may legitimately predate it (issue #1493 review).
            baseline_manifest = collect_manifest(
                baseline_root, extra_public_names, enforce_all=False
            )
            current_manifest = collect_manifest(repo_root, extra_public_names, enforce_all=True)

        breakages = compare_manifests(baseline_manifest, current_manifest)
        unapproved, approved = partition_allowed(breakages, allowances)
        stale = stale_allowances(breakages, allowances)
    except RuntimeError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    allowlist_display = allowlist_path or DEFAULT_ALLOWLIST
    # ``--check-stale`` promotes stale entries from informational to a gate.
    stale_blocks = args.check_stale and bool(stale)
    failed = bool(unapproved) or stale_blocks

    if args.json:
        print(
            json.dumps(
                {
                    "baseline_ref": baseline_ref,
                    "approved": [
                        {"break": asdict(breakage), "reason": allowance.reason}
                        for breakage, allowance in approved
                    ],
                    "unapproved": [asdict(item) for item in unapproved],
                    "stale_allowances": [asdict(allowance) for allowance in stale],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1 if failed else 0

    if unapproved:
        print(
            textwrap.dedent(
                f"""\
                Public API compatibility audit failed.
                Baseline: {baseline_ref}

                {_render_breakages("Unapproved compatibility breaks:", unapproved)}

                Add back-compat shims, or document an intentional break in
                {allowlist_display} with a reviewer-readable reason.
                """
            ).strip(),
            file=sys.stderr,
        )
        approved_text = _render_approved(approved)
        if approved_text:
            print("\n" + approved_text, file=sys.stderr)
    elif stale_blocks:
        # Compat surface is clean, but stale allowlist entries fail the gate
        # under --check-stale. Don't print an "OK:" line that contradicts the
        # non-zero exit; the stale report below carries the actionable message.
        print(
            f"Public API is compatible with {baseline_ref} "
            f"({len(approved)} reviewed break(s) allowlisted), "
            "but the allowlist has stale entries.",
            file=sys.stderr,
        )
    else:
        print(
            f"OK: public API is compatible with {baseline_ref} "
            f"({len(approved)} reviewed break(s) allowlisted)."
        )
        approved_text = _render_approved(approved)
        if approved_text:
            print(approved_text)

    if stale_blocks:
        print("\n" + _render_stale(stale, baseline_ref, allowlist_display), file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
