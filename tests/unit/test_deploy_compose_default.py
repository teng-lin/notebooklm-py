"""Guard the deploy compose's default image tag.

`deploy/docker-compose.yml` used to default the app image to a `set-version` sentinel that FAILS
when unset (a deliberate fail-loud). It now defaults to `latest` so a bare `docker compose up -d`
works for end users. This test pins that the default stays a *real, resolvable* tag shape (``latest``
or a PEP 440 version) — a future edit can't silently reintroduce a non-resolving default, and the
release workflow's version-bake (`sed` on ``${NOTEBOOKLM_MCP_VERSION:-latest}``) keeps matching.
"""

from __future__ import annotations

import re
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("could not locate repo root (no pyproject.toml above this file)")


def test_deploy_compose_app_image_default_tag_is_resolvable() -> None:
    compose = (_repo_root() / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    # The app service's image line: ...tenglin/notebooklm-mcp}:${NOTEBOOKLM_MCP_VERSION:-<default>}
    m = re.search(r"notebooklm-mcp\}:\$\{NOTEBOOKLM_MCP_VERSION:-([^}]+)\}", compose)
    assert m, "app image line not found / not in the expected ${NOTEBOOKLM_MCP_VERSION:-…} form"
    default = m.group(1)
    # Must be a real tag: `latest`, or a PEP 440-ish version — release segment plus optional
    # pre-release (a/b/rc/c), post-release (.postN), and/or dev-release (.devN):
    # 0.8.0 / 0.8.0b2 / 1.2.3rc1 / 1.2.3.post1 / 1.2.3.dev0.
    version_re = r"\d+\.\d+(\.\d+)?((a|b|c|rc)\d+)?(\.post\d+)?(\.dev\d+)?"
    assert default == "latest" or re.fullmatch(version_re, default), (
        f"deploy compose default tag {default!r} is not a resolvable tag "
        f"(expected 'latest' or a PEP 440 version) — a bare `docker compose up` would fail to pull"
    )
