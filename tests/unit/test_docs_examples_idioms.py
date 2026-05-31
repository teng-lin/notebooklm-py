"""Guardrail tests for copy-paste documentation idioms.

The README and the runnable ``docs/examples/*.py`` are the snippets users copy
first. They must demonstrate the *canonical* client idiom, not a deprecated one
that warns at runtime.

When this test fails, fix the doc/example (drop the ``await`` before
``NotebookLMClient.from_storage(...)``), not the test.

See ``docs/deprecations.md`` — awaiting ``from_storage(...)`` emits a
``DeprecationWarning`` (removed in v1.0); the canonical form is
``async with NotebookLMClient.from_storage(...) as client:``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
README_MD = REPO_ROOT / "README.md"
EXAMPLES_DIR = REPO_ROOT / "docs" / "examples"

# The deprecated idiom: awaiting ``from_storage`` inside the context manager.
DEPRECATED_AWAIT_IDIOM = "async with await NotebookLMClient.from_storage"


def _copy_paste_docs() -> list[Path]:
    """README plus every runnable example — the snippets users copy first."""
    return [README_MD, *sorted(EXAMPLES_DIR.glob("*.py"))]


@pytest.mark.parametrize(
    "doc",
    _copy_paste_docs(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_copy_paste_docs_use_canonical_from_storage(doc: Path) -> None:
    """README + examples must use ``async with NotebookLMClient.from_storage()``.

    The ``await``-prefixed form still works but emits a ``DeprecationWarning``;
    shipping it in the first snippets a user copies makes their very first run
    warn. The canonical no-``await`` form is documented in ``docs/python-api.md``
    (which deliberately keeps the deprecated form as a *labeled* example and is
    therefore excluded here).
    """
    text = doc.read_text(encoding="utf-8")
    assert DEPRECATED_AWAIT_IDIOM not in text, (
        f"{doc.relative_to(REPO_ROOT)} uses the deprecated "
        f"'{DEPRECATED_AWAIT_IDIOM}(...)' idiom; drop the 'await' so the "
        "snippet matches the canonical context-manager form."
    )
