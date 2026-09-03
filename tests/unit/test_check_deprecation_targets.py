"""Tests for ``scripts/check_deprecation_targets.py`` (issue #1214 part c).

The release gate fails if any ``warnings.warn`` / ``DeprecationWarning``
message under ``src/notebooklm/`` names the version currently in
``pyproject.toml`` as its *removal target*. A deprecation must never point at
the version shipping it.

Tests cover:

* The gate is GREEN against the live repository tree (the lapsed v0.6.0 shims
  were deleted by #1224, so ``LAPSED_ALLOWLIST`` is now empty and nothing
  trips the gate).
* Any allowlist entry is well-formed (cites a tracking issue and a version).
* A synthetic offender naming the current version is caught (rc 1).
* The ``removed in`` / ``will be removed in`` / ``scheduled for removal in``
  phrasings are all detected, with and without the ``v`` prefix.
* A deprecation naming a *different* version does not trip the gate.
* An allowlisted offender does not block; removing the offender makes the
  allowlist entry stale (rc 1).
* The immutable registered-deprecation table has exactly its literal keys, valid
  semantic versions and structurally resolvable public replacements.
* Registered specs and callsites are a one-to-one set; missing, stale,
  duplicate, dynamic, or lapsed entries fail closed without importing package
  code.
* Missing / malformed ``pyproject.toml`` returns rc 2.

Script is imported via spec-loading to match the convention used by
``test_check_action_pinning.py`` (``scripts/`` is not a Python package).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
from textwrap import dedent

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_deprecation_targets.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_deprecation_targets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


def _write_pyproject(tmp_path: Path, version: str) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        dedent(
            f"""
            [project]
            name = "example"
            version = "{version}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return pyproject


def _run(script, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = script.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Live-repo guard
# ---------------------------------------------------------------------------


def test_live_repository_passes_the_gate(script) -> None:
    """The real tree is green: lapsed shims are allowlisted; nothing else trips."""
    rc, out, err = _run(script, [])
    assert rc == 0, err
    assert "OK" in out


def test_lapsed_allowlist_entries_are_well_formed(script) -> None:
    """Any allowlist entry must cite a tracking issue and name a version.

    The allowlist is legitimately empty once the lapsed shims are deleted (the
    #1213 v0.6.0 entries were dropped when their tracking PR removed the
    shims). When future lapsed shims are added back, each entry must still be
    well-formed (positive int issue, non-empty version + reason, src path) —
    kept generic rather than pinning specific issue/version values.
    """
    for entry in script.LAPSED_ALLOWLIST:
        assert isinstance(entry.issue, int) and entry.issue > 0, entry.path
        assert isinstance(entry.version, str) and entry.version, entry.path
        assert isinstance(entry.reason, str) and entry.reason, entry.path
        assert entry.path.startswith("src/notebooklm/"), entry.path


# ---------------------------------------------------------------------------
# Synthetic-tree behaviour (monkeypatch SRC_ROOT/REPO_ROOT onto the module)
# ---------------------------------------------------------------------------


@pytest.fixture()
def synthetic(script, tmp_path, monkeypatch):
    """Point the script's scan at an isolated synthetic source tree."""
    src = tmp_path / "src" / "notebooklm"
    src.mkdir(parents=True)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "SRC_ROOT", src)
    # Neutralise the real allowlist so synthetic offenders are not masked.
    monkeypatch.setattr(script, "LAPSED_ALLOWLIST", ())
    monkeypatch.setattr(script, "_ALLOWLIST_BY_KEY", {})
    return src


def _spec_entry(
    key: str,
    *,
    replacement: str = '"notebooklm.NotebookLMClient.from_storage"',
    since: str = '"0.9.0"',
    removal: str = '"1.0"',
) -> str:
    return dedent(
        f"""
        {key!r}: DeprecationSpec(
            key={key!r},
            message="deprecated auth storage path",
            category=DeprecationWarning,
            replacement={replacement},
            since={since},
            removal={removal},
            stacklevel=3,
        ),
        """
    )


def _install_registered_tree(
    src: Path,
    *,
    entries: list[str] | None = None,
    calls: list[str] | None = None,
    immutable: bool = True,
) -> None:
    entries = entries or [
        _spec_entry("auth_tokens_flat_cookies"),
        _spec_entry("auth_tokens_from_storage"),
        _spec_entry("auth_tokens_sync_storage_construction"),
        _spec_entry(
            "client_rpc_call_web",
            replacement='"notebooklm.raw.WebRawAPI.call"',
        ),
        _spec_entry(
            "client_rpc_call_android",
            replacement='"notebooklm.raw.AndroidRawAPI.unary"',
        ),
    ]
    calls = calls or [
        'warn_registered_deprecation("auth_tokens_flat_cookies")',
        'warn_registered_deprecation("auth_tokens_from_storage")',
        'warn_registered_deprecation("auth_tokens_sync_storage_construction")',
        'warn_registered_deprecation("client_rpc_call_web")',
        'warn_registered_deprecation("client_rpc_call_android")',
    ]
    (src / "__init__.py").write_text("from .client import NotebookLMClient\n", encoding="utf-8")
    (src / "client.py").write_text(
        dedent(
            """
            class NotebookLMClient:
                @classmethod
                def from_storage(cls):
                    return cls()
            """
        ),
        encoding="utf-8",
    )
    (src / "raw.py").write_text(
        dedent(
            """
            class WebRawAPI:
                async def call(self):
                    pass

            class AndroidRawAPI:
                async def unary(self):
                    pass
            """
        ),
        encoding="utf-8",
    )
    wrapper = "MappingProxyType({" if immutable else "{"
    close = "})" if immutable else "}"
    registry = (
        "from types import MappingProxyType\n\n"
        "class DeprecationSpec:\n"
        "    pass\n\n"
        f"DEPRECATION_SPECS = {wrapper}\n" + "".join(entries) + f"{close}\n"
    )
    (src / "_deprecation.py").write_text(registry, encoding="utf-8")
    (src / "_auth").mkdir()
    (src / "_auth" / "tokens.py").write_text("\n".join(calls) + "\n", encoding="utf-8")


def test_registered_deprecation_specs_and_callsites_are_validated(
    script, synthetic, tmp_path
) -> None:
    _install_registered_tree(synthetic)
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 0, err
    assert "OK" in out


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ('""', "replacement must be a non-empty string literal"),
        ('"notebooklm.Missing.value"', "replacement does not resolve"),
    ],
)
def test_registered_replacement_must_be_nonempty_and_resolve_without_imports(
    script, synthetic, tmp_path, replacement, expected
) -> None:
    _install_registered_tree(
        synthetic,
        entries=[
            _spec_entry("auth_tokens_flat_cookies"),
            _spec_entry("auth_tokens_from_storage", replacement=replacement),
            _spec_entry("auth_tokens_sync_storage_construction"),
        ],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert expected in err


def test_registered_replacement_resolves_through_public_reexport_chain(script, synthetic) -> None:
    (synthetic / "__init__.py").write_text(
        "from .auth import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "auth.py").write_text(
        "from ._auth.tokens import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "_auth").mkdir()
    (synthetic / "_auth" / "tokens.py").write_text(
        "class AuthTokens:\n    @property\n    def jar(self):\n        return None\n",
        encoding="utf-8",
    )

    assert script._replacement_resolves("notebooklm.AuthTokens.jar")


def test_registered_replacement_resolves_nested_relative_reexports(script, synthetic) -> None:
    (synthetic / "__init__.py").write_text(
        "from .pkg import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "pkg").mkdir()
    (synthetic / "pkg" / "__init__.py").write_text(
        "from .tokens import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "pkg" / "tokens.py").write_text(
        "class AuthTokens:\n    @property\n    def jar(self):\n        return None\n",
        encoding="utf-8",
    )

    assert script._replacement_resolves("notebooklm.AuthTokens.jar")


def test_registered_replacement_does_not_escape_nested_relative_package(script, synthetic) -> None:
    (synthetic / "__init__.py").write_text(
        "from .pkg import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "pkg").mkdir()
    (synthetic / "pkg" / "__init__.py").write_text(
        "from .tokens import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "tokens.py").write_text(
        "class AuthTokens:\n    @property\n    def jar(self):\n        return None\n",
        encoding="utf-8",
    )

    assert not script._replacement_resolves("notebooklm.AuthTokens.jar")


def test_registered_replacement_reexport_cycle_is_rejected(script, synthetic) -> None:
    (synthetic / "__init__.py").write_text(
        "from .pkg import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "pkg").mkdir()
    (synthetic / "pkg" / "__init__.py").write_text(
        "from .a import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "pkg" / "a.py").write_text(
        "from .b import AuthTokens\n",
        encoding="utf-8",
    )
    (synthetic / "pkg" / "b.py").write_text(
        "from .a import AuthTokens\n",
        encoding="utf-8",
    )

    assert not script._replacement_resolves("notebooklm.AuthTokens.jar")


def test_duplicate_registered_spec_key_is_rejected(script, synthetic, tmp_path) -> None:
    _install_registered_tree(
        synthetic,
        entries=[
            _spec_entry("auth_tokens_flat_cookies"),
            _spec_entry("auth_tokens_from_storage"),
            _spec_entry("auth_tokens_from_storage"),
            _spec_entry("auth_tokens_sync_storage_construction"),
        ],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert "duplicate deprecation spec key" in err


def test_required_registered_spec_key_cannot_disappear(script, synthetic, tmp_path) -> None:
    _install_registered_tree(
        synthetic,
        entries=[_spec_entry("auth_tokens_from_storage")],
        calls=['warn_registered_deprecation("auth_tokens_from_storage")'],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert "DEPRECATION_SPECS keys differ" in err


@pytest.mark.parametrize(
    ("field", "value"),
    [("since", '"v0.9.0"'), ("since", "FUTURE_VERSION"), ("removal", '"next"')],
)
def test_registered_versions_are_literal_semantic_versions(
    script, synthetic, tmp_path, field, value
) -> None:
    kwargs = {field: value}
    _install_registered_tree(
        synthetic,
        entries=[
            _spec_entry("auth_tokens_flat_cookies"),
            _spec_entry("auth_tokens_from_storage", **kwargs),
            _spec_entry("auth_tokens_sync_storage_construction"),
        ],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert f"auth_tokens_from_storage.{field}" in err


@pytest.mark.parametrize("shipping_version", ["1.0", "1.0.1", "1.1.0"])
def test_registered_removal_must_follow_shipping_release(
    script, synthetic, tmp_path, shipping_version
) -> None:
    _install_registered_tree(
        synthetic,
        entries=[
            _spec_entry("auth_tokens_flat_cookies"),
            _spec_entry("auth_tokens_from_storage"),
            _spec_entry("auth_tokens_sync_storage_construction"),
        ],
    )
    pyproject = _write_pyproject(tmp_path, shipping_version)
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert f"removal 1.0 is not after shipping version {shipping_version}" in err


@pytest.mark.parametrize(
    ("since", "removal"),
    [("1.0", "1.0"), ("1.1", "1.0"), ("2.0.1", "2.0")],
)
def test_registered_since_must_precede_removal(script, synthetic, tmp_path, since, removal) -> None:
    _install_registered_tree(
        synthetic,
        entries=[
            _spec_entry("auth_tokens_flat_cookies"),
            _spec_entry(
                "auth_tokens_from_storage",
                since=repr(since),
                removal=repr(removal),
            ),
            _spec_entry("auth_tokens_sync_storage_construction"),
        ],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert "auth_tokens_from_storage.since must precede removal" in err


def test_registered_spec_without_callsite_is_stale(script, synthetic, tmp_path) -> None:
    _install_registered_tree(
        synthetic,
        calls=['warn_registered_deprecation("auth_tokens_from_storage")'],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert "stale deprecation spec has no callsite" in err


def test_registered_callsite_without_spec_is_rejected(script, synthetic, tmp_path) -> None:
    _install_registered_tree(
        synthetic,
        calls=[
            'warn_registered_deprecation("auth_tokens_flat_cookies")',
            'warn_registered_deprecation("auth_tokens_from_storage")',
            'warn_registered_deprecation("auth_tokens_sync_storage_construction")',
            'warn_registered_deprecation("unregistered")',
        ],
    )
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert "registered callsite has no spec: unregistered" in err


def test_registered_table_must_remain_immutable(script, synthetic, tmp_path) -> None:
    _install_registered_tree(synthetic, immutable=False)
    pyproject = _write_pyproject(tmp_path, "0.8.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1
    assert "must be one literal MappingProxyType dictionary" in err


@pytest.mark.parametrize(
    "phrase",
    [
        "will be removed in v0.7.0",
        "will be removed in 0.7.0",
        "removed in v0.7.0",
        "scheduled for removal in v0.7.0",
        "removal in 0.7.0",
    ],
)
def test_offender_naming_current_version_is_caught(script, synthetic, tmp_path, phrase) -> None:
    (synthetic / "_feature.py").write_text(
        dedent(
            f"""
            import warnings

            def f():
                warnings.warn(
                    "old_param is deprecated and {phrase}; use new_param.",
                    DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "_feature.py" in err
    assert "removal target" in err


def test_deprecation_naming_other_version_does_not_trip(script, synthetic, tmp_path) -> None:
    (synthetic / "_feature.py").write_text(
        dedent(
            """
            import warnings

            def f():
                warnings.warn(
                    "old_param is deprecated and will be removed in v1.0.0.",
                    DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, out, _err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 0, out
    assert "OK" in out


def test_keyword_message_argument_is_scanned(script, synthetic, tmp_path) -> None:
    """A ``warnings.warn(message=...)`` keyword form must not bypass the gate."""
    (synthetic / "_feature.py").write_text(
        dedent(
            """
            import warnings

            def f():
                warnings.warn(
                    message="old_param will be removed in v0.7.0.",
                    category=DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "_feature.py" in err


def test_direct_deprecationwarning_construction_is_scanned(script, synthetic, tmp_path) -> None:
    (synthetic / "_feature.py").write_text(
        dedent(
            """
            def f():
                raise DeprecationWarning(
                    "feature X scheduled for removal in v0.7.0."
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "_feature.py" in err


def test_allowlisted_offender_does_not_block(script, synthetic, tmp_path, monkeypatch) -> None:
    (synthetic / "_legacy.py").write_text(
        dedent(
            """
            import warnings

            def f():
                warnings.warn(
                    "legacy will be removed in v0.7.0.",
                    DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    entry = script._LapsedEntry("src/notebooklm/_legacy.py", "0.7.0", 9999, "tracked elsewhere")
    monkeypatch.setattr(script, "LAPSED_ALLOWLIST", (entry,))
    monkeypatch.setattr(script, "_ALLOWLIST_BY_KEY", {entry.key: entry})
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, out, _err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 0, out
    assert "allowlisted" in out


def test_stale_allowlist_entry_is_reported(script, synthetic, tmp_path, monkeypatch) -> None:
    # No source file names v0.7.0, but an allowlist entry claims one does.
    (synthetic / "_clean.py").write_text("x = 1\n", encoding="utf-8")
    entry = script._LapsedEntry("src/notebooklm/_legacy.py", "0.7.0", 9999, "gone")
    monkeypatch.setattr(script, "LAPSED_ALLOWLIST", (entry,))
    monkeypatch.setattr(script, "_ALLOWLIST_BY_KEY", {entry.key: entry})
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "Stale" in err


# ---------------------------------------------------------------------------
# Argument / parse errors
# ---------------------------------------------------------------------------


def test_missing_pyproject_returns_rc_2(script, tmp_path) -> None:
    rc, _out, err = _run(script, ["--pyproject", str(tmp_path / "nope.toml")])
    assert rc == 2
    assert "not found" in err


def test_malformed_pyproject_returns_rc_2(script, tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")  # no version
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 2
    assert "project.version" in err
