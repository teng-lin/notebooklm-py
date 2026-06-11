"""Auth-source consolidation guard for P3.T3.

The env-var name is intentionally centralized in
``cli/services/auth_source.py``. Other CLI modules must import
``AUTH_JSON_ENV_NAME`` / ``has_env_auth_json`` instead of re-declaring
the literal in source comments, docs, or logic. This keeps the original
auth-source consolidation grep gate enforceable.
"""

from __future__ import annotations

from pathlib import Path

CLI_ROOTS = (
    Path("src/notebooklm/cli"),
    Path("src/notebooklm/notebooklm_cli.py"),
)
AUTH_SOURCE_PATH = Path("src/notebooklm/cli/services/auth_source.py")
ENV_LITERAL = "NOTEBOOKLM_AUTH_JSON"


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in CLI_ROOTS:
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_auth_json_env_literal_is_centralized() -> None:
    offenders = [
        path
        for path in _source_files()
        if path != AUTH_SOURCE_PATH and ENV_LITERAL in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{ENV_LITERAL} must only be declared in {AUTH_SOURCE_PATH}; "
        "other CLI modules should import AUTH_JSON_ENV_NAME or describe it generically. "
        f"Offenders: {[str(path) for path in offenders]}"
    )
