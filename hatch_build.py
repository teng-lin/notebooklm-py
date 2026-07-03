"""Hatchling build hook: bake the git commit into the build as ``_commit.py``.

Runs for both the sdist and the wheel so the commit survives the standard
release path (``python -m build`` builds the wheel *from* the sdist, which has
no ``.git``):

- **sdist** (built from a checkout) embeds ``src/notebooklm/_commit.py`` into
  the tarball, so a wheel later built from that sdist still carries the commit.
- **wheel** (built directly from a checkout) embeds ``notebooklm/_commit.py``.
  When built from a carried-forward sdist the hook finds no ``.git`` and skips —
  the file the sdist carried is packaged normally.

``notebooklm._version_info`` reads it back at runtime so ``--version`` /
``server_info`` report the commit even with no ``.git`` around.

Only trusts git when ``.git`` sits at the build root — mirrors the runtime
guard so an sdist unpacked *inside another repo* can't bake in the enclosing
repo's HEAD. Every step is best-effort: any failure → no file → bare version.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CommitBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        # Only trust git if THIS root is the repo — not an enclosing one.
        if not (root / ".git").exists():
            return
        commit = _git_commit(str(root))
        if not commit:
            return
        gen = root / "build" / "_commit.py"
        try:
            gen.parent.mkdir(parents=True, exist_ok=True)
            gen.write_text(f'COMMIT = "{commit}"\n', encoding="utf-8")
        except OSError:
            return  # best-effort: read-only checkout etc. → bare version
        # sdist keeps the src layout; wheel flattens src/notebooklm -> notebooklm.
        dest = (
            "src/notebooklm/_commit.py" if self.target_name == "sdist" else "notebooklm/_commit.py"
        )
        build_data.setdefault("force_include", {})[str(gen)] = dest


def _git_commit(root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None
