"""Lazy package-export resolution for ``_artifact`` and ``_source``.

Both packages keep historical package-level names that now live in the web
backend, resolved through a module ``__getattr__`` so importing the neutral
package never pulls ``_web`` in. These cases pin every advertised name, the
caching that follows the first access, and the ``AttributeError`` for anything
else — a silently-broken lazy branch would surface as an import error only in
whichever adapter still used the old spelling.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import subprocess
import sys

import pytest

from notebooklm import _artifact, _source

_ARTIFACT_LAZY = (
    "ArtifactDownloadService",
    "ArtifactListingService",
    "find_artifact_row_by_id",
    "iter_artifact_rows",
    "generation",
    "listing",
    "payloads",
)


@pytest.mark.parametrize("name", _ARTIFACT_LAZY)
def test_artifact_lazy_names_resolve_to_their_web_owners(name: str) -> None:
    value = getattr(_artifact, name)

    assert value is not None
    # Resolution is memoised into the module globals, so a second access is a
    # plain attribute read rather than another import.
    assert _artifact.__dict__[name] is value
    assert getattr(_artifact, name) is value


def test_artifact_lazy_names_match_the_modules_they_delegate_to() -> None:
    from notebooklm._web.artifact import generation, listing
    from notebooklm._web.artifact.downloads import ArtifactDownloadService
    from notebooklm._web.params import artifacts

    assert _artifact.generation is generation
    assert _artifact.listing is listing
    assert _artifact.payloads is artifacts
    assert _artifact.ArtifactDownloadService is ArtifactDownloadService
    assert _artifact.ArtifactListingService is listing.ArtifactListingService
    assert _artifact.find_artifact_row_by_id is listing.find_artifact_row_by_id
    assert _artifact.iter_artifact_rows is listing.iter_artifact_rows


def test_artifact_rejects_an_unknown_attribute() -> None:
    missing = "nope"
    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        getattr(_artifact, missing)


def test_artifact_all_is_fully_resolvable() -> None:
    """Every advertised name must actually resolve, eager or lazy."""
    for name in _artifact.__all__:
        assert getattr(_artifact, name) is not None


@pytest.mark.parametrize("name", sorted(_source._MODULE_EXPORTS))
def test_source_module_exports_resolve_to_the_named_module(name: str) -> None:
    value = getattr(_source, name)

    assert value is importlib.import_module(_source._MODULE_EXPORTS[name])
    assert _source.__dict__[name] is value


def test_source_batch_resolves_to_the_neutral_submodule() -> None:
    """``_source.batch`` must always resolve to the neutral submodule.

    The declared ``_MODULE_EXPORTS['batch']`` matches the real neutral
    submodule rather than pointing at ``_web.sources.batch``.
    """
    import notebooklm._source.batch as neutral_batch

    assert _source.batch is neutral_batch
    assert _source._MODULE_EXPORTS["batch"] == "notebooklm._source.batch"


def test_accessing_source_batch_adds_no_additional_web_modules() -> None:
    """Accessing ``_source.batch`` must not import additional web backend modules."""
    probe = (
        "import sys, json; import notebooklm; "
        "import notebooklm._source; "
        "before = {n for n in sys.modules if n.startswith('notebooklm._web')}; "
        "_ = notebooklm._source.batch; "
        "after = {n for n in sys.modules if n.startswith('notebooklm._web')}; "
        "print(json.dumps(sorted(after - before)))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert json.loads(result.stdout) == []


@pytest.mark.parametrize("name", sorted(_source._SYMBOL_EXPORTS))
def test_source_symbol_exports_resolve_to_the_named_attribute(name: str) -> None:
    module_name, attribute = _source._SYMBOL_EXPORTS[name]

    value = getattr(_source, name)

    assert value is getattr(importlib.import_module(module_name), attribute)
    assert _source.__dict__[name] is value


def test_source_rejects_an_unknown_attribute() -> None:
    missing = "nope"
    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        getattr(_source, missing)


def test_source_dir_advertises_the_lazy_exports() -> None:
    listed = dir(_source)

    assert set(_source._MODULE_EXPORTS) <= set(listed)
    assert set(_source._SYMBOL_EXPORTS) <= set(listed)
    assert listed == sorted(listed)


def test_source_all_is_fully_resolvable() -> None:
    for name in _source.__all__:
        assert getattr(_source, name) is not None


def test_importing_the_neutral_source_package_adds_no_web_modules() -> None:
    """Importing ``_source`` itself must not pull ``_web.sources`` in.

    Measured as a delta against ``import notebooklm``, which eagerly loads the
    web backend on its own — the lazy map only buys isolation for this
    submodule, and asserting more would pass for the wrong reason.

    Runs in a subprocess: evicting ``notebooklm._web*`` from this process's
    ``sys.modules`` to measure it in-process would hand every later test a
    second copy of those classes and silently break unrelated ``isinstance``
    checks elsewhere in the suite.
    """
    probe = (
        "import sys, json; import notebooklm; "
        "before = {n for n in sys.modules if n.startswith('notebooklm._web')}; "
        "import notebooklm._source; "
        "after = {n for n in sys.modules if n.startswith('notebooklm._web')}; "
        "print(json.dumps(sorted(after - before)))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert json.loads(result.stdout) == []


def test_the_neutral_source_package_has_no_static_web_import() -> None:
    """The lazy map exists so this file names ``_web`` only inside strings."""
    tree = ast.parse(inspect.getsource(_source))

    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert not [name for name in imported if "_web" in name]
