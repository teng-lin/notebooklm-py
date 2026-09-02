"""Lazy package-level codec aliases in ``notebooklm._android.codecs``.

The package initializer resolves the older private aliases through
``__getattr__`` so importing one adapter does not fan out into every generated
protobuf module. Concrete adapters import their codec submodules directly, so
nothing in the runtime exercises this path — these cases do.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys

import pytest

from notebooklm._android import codecs

_NOTEBOOK_ALIASES = ("decode_project", "map_get_project_error", "message_to_known_dict")
_SOURCE_ALIASES = ("decode_source", "decode_sources")


@pytest.mark.parametrize("name", _NOTEBOOK_ALIASES)
def test_notebook_aliases_resolve_to_the_notebooks_codec(name: str) -> None:
    from notebooklm._android.codecs import notebooks

    assert getattr(codecs, name) is getattr(notebooks, name)


@pytest.mark.parametrize("name", _SOURCE_ALIASES)
def test_source_aliases_resolve_to_the_sources_codec(name: str) -> None:
    from notebooklm._android.codecs import sources

    assert getattr(codecs, name) is getattr(sources, name)


def test_an_unknown_alias_raises_attribute_error() -> None:
    missing = "decode_nothing"

    with pytest.raises(AttributeError, match=missing):
        getattr(codecs, missing)


def test_every_advertised_alias_resolves() -> None:
    """``__all__`` and the two resolver sets must not drift apart."""
    assert set(codecs.__all__) == set(_NOTEBOOK_ALIASES) | set(_SOURCE_ALIASES)
    for name in codecs.__all__:
        assert callable(getattr(codecs, name))


def test_importing_the_codec_package_pulls_in_no_generated_protobufs() -> None:
    """The whole point of the lazy aliases — measured as an import delta.

    Runs in a subprocess so the measurement cannot be perturbed by whatever
    earlier tests in this worker already imported, and so it never has to evict
    modules from this process (which would hand later tests a second copy of
    those classes).
    """
    probe = (
        "import sys, json; "
        "import notebooklm._android.codecs; "
        "print(json.dumps(sorted(n for n in sys.modules "
        "if n.startswith('notebooklm._android.proto'))))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert json.loads(result.stdout) == []


def test_resolving_an_alias_is_what_imports_the_submodule() -> None:
    """Complements the test above: the fan-out happens on access, not import."""
    probe = (
        "import sys, json; "
        "import notebooklm._android.codecs as c; "
        "before = 'notebooklm._android.codecs.sources' in sys.modules; "
        "c.decode_source; "
        "after = 'notebooklm._android.codecs.sources' in sys.modules; "
        "print(json.dumps([before, after]))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert json.loads(result.stdout) == [False, True]


def test_resolution_survives_a_reimport_of_the_package() -> None:
    """``__getattr__`` holds no cached state of its own."""
    from notebooklm._android.codecs import sources

    reloaded = importlib.reload(codecs)

    assert reloaded.decode_source is sources.decode_source
