"""Transport-neutral doctor diagnostics business logic.

This is the Click-free core of ``cli/doctor_cmd.py``: it runs the install
checks (migration / profile-dir / auth / config / headless-reauth readiness),
optionally applies the
automatic fixes, aggregates the overall pass/fail health, and returns a typed
:class:`DoctorReport`. Every transport adapter (the Click CLI today, a future
HTTP / FastMCP surface tomorrow) drives :func:`run_checks` and renders the
report into its own surface + exit-code policy; the Rich table + remediation
hints stay in the CLI.

The path helpers the checks need (``get_path_info`` / ``get_home_dir`` /
``get_profile_dir`` / ``get_storage_path`` / ``get_config_path``) plus the
``headless_reauth_check`` readiness closure are
**injected** via a :class:`DoctorPaths` bundle rather than imported, so this
core never reaches into ``notebooklm.paths`` directly and the CLI's
``patch("notebooklm.cli.doctor_cmd.get_storage_path", ...)`` test seam keeps
landing (the CLI reads the helpers off its own module at call time and forwards
them here). Any unexpected ``OSError`` from a path helper propagates out of
:func:`run_checks` so the CLI's ``handle_errors`` envelope can wrap it.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Detail emitted for the profile dir on Windows, where POSIX mode bits are not
# the access-control mechanism (see :func:`_check_profile_dir`). Stated
# explicitly rather than reusing the bare POSIX "pass" detail so the report
# says the check was *evaluated and not applicable*, not silently skipped.
_WINDOWS_PROFILE_DIR_DETAIL = (
    "Windows: access controlled by inherited NTFS ACLs; POSIX mode not enforced"
)


def _is_windows(platform: str | None) -> bool:
    """Resolve the effective platform, honouring an injected ``platform`` override.

    ``None`` — and *only* ``None`` — means "ask the host", read at call time so
    tests can exercise the Windows carve-out (see :func:`_check_profile_dir`) on
    any runner instead of skipping it off-Windows. An explicitly passed value is
    honoured verbatim, so an empty string reads as "not Windows" rather than
    silently falling back to the host's platform.
    """
    return (sys.platform if platform is None else platform) == "win32"


@dataclass(frozen=True)
class DoctorPaths:
    """Injected path-resolver collaborators the checks depend on.

    The CLI builds this from its own ``doctor_cmd``-namespace helpers (read at
    call time) so the ``patch("...doctor_cmd.get_storage_path")`` seam lands.

    ``headless_reauth_check`` is an injected ``() -> {"status", "detail"}``
    closure rather than a path helper: the L3 readiness probe lives in
    ``notebooklm._browser.headless_reauth`` (a private runtime sibling this
    transport-neutral core must NOT import — see the ``_app`` boundary lint),
    so the adapter that *may* import ``_auth`` supplies the probe and maps its
    credential-free outcome to the standard check shape.
    """

    get_path_info: Callable[[], dict[str, Any]]
    get_home_dir: Callable[..., Path]
    get_profile_dir: Callable[..., Path]
    get_storage_path: Callable[[], Path]
    get_config_path: Callable[[], Path]
    headless_reauth_check: Callable[[], dict[str, str]]


@dataclass(frozen=True)
class DoctorReport:
    """Typed outcome of :func:`run_checks`.

    ``checks`` mirrors the historical ``{name: {"status", "detail"}}`` mapping
    so the CLI can rebuild its ``--json`` envelope byte-for-byte. ``has_failures``
    is computed from the *final* check states (after any fixes) and drives the
    non-zero exit.
    """

    profile: str
    profile_source: str
    checks: dict[str, dict[str, str]]
    fixes_applied: list[str] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return any(c["status"] == "fail" for c in self.checks.values())


def _check_migration(home: Path) -> dict[str, str]:
    profiles_dir = home / "profiles"
    has_legacy = any(
        (home / name).exists() for name in ("storage_state.json", "context.json", "browser_profile")
    )
    has_profiles = profiles_dir.exists()

    if has_profiles and not has_legacy:
        return {"status": "pass", "detail": "complete"}
    if has_legacy and not has_profiles:
        return {"status": "fail", "detail": "legacy layout detected"}
    if has_legacy and has_profiles:
        return {"status": "warn", "detail": "legacy files remain alongside profiles"}
    return {"status": "pass", "detail": "clean (no legacy files)"}


def _check_profile_dir(profile_dir: Path, *, platform: str | None = None) -> dict[str, str]:
    """Check the profile directory exists and (on POSIX) is 0o700.

    The permission half is **platform-conditional on purpose**. The writer
    (``notebooklm.paths._ensure_dir``) deliberately skips ``mode=`` / ``chmod``
    on Windows: pre-3.13 CPython ignores ``mode=`` in ``mkdir()``, and 3.13+
    turns it into Windows ACLs restrictive enough to block other same-user
    processes, so the directory is left inheriting the parent's ACLs. Comparing
    POSIX bits there reported a permanent, unactionable ``warn`` describing the
    exact state we create on purpose — and ``--fix`` could not clear it,
    because ``mkdir(mode=…)`` / ``chmod`` do not move those bits on Windows
    either (issue #2046, surfaced in #2025).

    ``platform`` defaults to :data:`sys.platform`, read at call time; tests
    inject ``"win32"`` directly rather than skipping off-Windows.
    """
    if not profile_dir.exists():
        return {"status": "fail", "detail": f"{profile_dir} not found"}
    # Existence alone is not enough. A plain file sitting at the profile path
    # makes the whole profile unusable — nothing can write ``storage_state.json``
    # beneath it — and the mode check would misdiagnose it as a permissions
    # problem (or, on the Windows branch, pass it outright). Report the real
    # cause instead. ``--fix`` deliberately does NOT repair this: clearing it
    # means deleting a user file.
    if not profile_dir.is_dir():
        return {"status": "fail", "detail": f"{profile_dir} exists but is not a directory"}
    if _is_windows(platform):
        return {"status": "pass", "detail": f"{profile_dir} ({_WINDOWS_PROFILE_DIR_DETAIL})"}
    perms = profile_dir.stat().st_mode & 0o777
    if perms == 0o700:
        return {"status": "pass", "detail": str(profile_dir)}
    return {
        "status": "warn",
        "detail": f"{profile_dir} (permissions: {oct(perms)}, expected: 0o700)",
    }


def _check_auth(storage_path: Path) -> dict[str, str]:
    if not storage_path.exists():
        return {"status": "fail", "detail": "not authenticated"}
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("storage root is not an object")
        cookies = data.get("cookies", [])
        if not isinstance(cookies, list):
            raise ValueError("cookies is not a list")
        # Reuse the shared, name-robust extractor (drops non-dict rows and
        # nameless / empty-name / non-str-name entries) rather than a bare
        # ``{c.get("name") ...}`` set — that would fold a nameless row in as a
        # ``None`` member. Import is function-local so importing this neutral
        # core never pulls the auth facade on the common path (mirrors the
        # ``_auth`` import deferral in ``cli/doctor_cmd._headless_reauth_check``).
        from ..auth import cookie_names_from_storage

        # Count actual cookie *entries*, not unique names: the same name can
        # legitimately appear on multiple domains, so ``len(cookie_names)`` would
        # under-report the file's cookie count in the "N cookies" detail.
        cookie_count = sum(isinstance(c, dict) for c in cookies)
        cookie_names = cookie_names_from_storage(data)
        if "SID" not in cookie_names:
            return {"status": "fail", "detail": "SID cookie missing"}
        # SID alone does not make a session usable. Google's homepage check also
        # requires __Secure-1PSIDTS — the rotating freshness partner of
        # __Secure-1PSID and the second half of the Tier-1 MINIMUM_REQUIRED_COOKIES
        # set every real RPC enforces. It legitimately rotates and can be re-minted
        # at runtime (RotateCookies), so a static, offline probe like doctor must
        # not call its absence a hard failure — that would false-negative a
        # recoverable session. But its absence is the #1 reason a session that
        # looks authenticated is actually unusable (issue #1753; common on Windows,
        # where Chrome 127+ App-Bound Encryption blocks --browser-cookies decryption
        # and automated login can be served a session without the token-binding
        # cookie). Surface it as a warn so doctor stops greenlighting an unusable
        # session, without flipping the exit code on a session that may still heal.
        if "__Secure-1PSIDTS" not in cookie_names:
            return {
                "status": "warn",
                "detail": (
                    f"SID present but __Secure-1PSIDTS missing ({cookie_count} cookies); "
                    "the session may be unusable until the cookie is refreshed. "
                    "Re-run 'notebooklm login'; on Windows (Chrome 127+ App-Bound "
                    "Encryption) use '--browser-cookies firefox' or set up "
                    "'notebooklm login --master-token'."
                ),
            }
        return {
            "status": "pass",
            "detail": f"local auth cookies present ({cookie_count} cookies)",
        }
    except (json.JSONDecodeError, OSError, ValueError) as e:
        return {"status": "fail", "detail": f"invalid storage file: {e}"}


def _check_config(config_path: Path, get_profile_dir: Callable[..., Path]) -> dict[str, str]:
    if not config_path.exists():
        return {"status": "pass", "detail": "not present (using defaults)"}
    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config_data, dict):
            raise ValueError("config root is not an object")
        default_profile = config_data.get("default_profile")
        if default_profile and isinstance(default_profile, str):
            try:
                profile_exists = get_profile_dir(default_profile).exists()
            except ValueError:
                profile_exists = False
            if profile_exists:
                return {
                    "status": "pass",
                    "detail": f"valid (default_profile: {default_profile})",
                }
            return {
                "status": "warn",
                "detail": f"default_profile '{default_profile}' does not exist",
            }
        return {"status": "pass", "detail": "valid (no default_profile set)"}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        return {"status": "fail", "detail": f"invalid: {e}"}


def _apply_fixes(
    checks: dict[str, dict[str, str]],
    home: Path,
    profile_dir: Path,
    migrate_to_profiles: Callable[[], bool],
    *,
    platform: str | None = None,
) -> list[str]:
    """Apply automatic fixes for detected issues (mutates ``checks`` in place).

    ``platform`` defaults to :data:`sys.platform`, read at call time. It gates
    the same POSIX-mode carve-out :func:`_check_profile_dir` uses so ``--fix``
    never *claims* a permission change it cannot make on Windows.
    """
    fixes: list[str] = []
    is_windows = _is_windows(platform)

    # Fix migration (both "fail" = no profiles dir, and "warn" = partial migration)
    if checks["migration"]["status"] in ("fail", "warn"):
        if migrate_to_profiles():
            fixes.append("Migrated legacy layout to profiles/default/")
            checks["migration"] = {"status": "pass", "detail": "complete (just migrated)"}
            if profile_dir.exists():
                checks["profile_dir"] = _check_profile_dir(profile_dir, platform=platform)

    # Fix missing profile directory. The mode= split mirrors
    # ``notebooklm.paths._ensure_dir`` (duplicated rather than imported: this
    # neutral core must not reach into ``notebooklm.paths`` — see the module
    # docstring). Passing mode=0o700 on Windows is at best ignored and at worst
    # applies an over-restrictive ACL, so we let Windows inherit instead.
    #
    # Guarded on the path being absent, not merely on the ``fail`` status: the
    # check also fails when a plain FILE occupies the profile path, and
    # ``mkdir(exist_ok=True)`` only tolerates an existing *directory* — it would
    # raise ``FileExistsError`` out of ``doctor --fix``. Repairing that case
    # means deleting a user file, so the failure is left standing for the human.
    if checks["profile_dir"]["status"] == "fail" and not profile_dir.exists():
        if is_windows:
            profile_dir.mkdir(parents=True, exist_ok=True)
        else:
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fixes.append(f"Created profile directory: {profile_dir}")
        checks["profile_dir"] = _check_profile_dir(profile_dir, platform=platform)

    # Fix permissions. Unreachable on Windows — ``_check_profile_dir`` never
    # returns the POSIX-permissions warn there — but the guard is explicit so
    # the no-op ``chmod`` can never be reached by a future check change.
    if (
        not is_windows
        and checks["profile_dir"]["status"] == "warn"
        and "permissions" in checks["profile_dir"]["detail"]
    ):
        profile_dir.chmod(0o700)
        fixes.append(f"Fixed permissions on {profile_dir}")
        checks["profile_dir"] = _check_profile_dir(profile_dir, platform=platform)

    return fixes


def run_checks(*, fix: bool, paths: DoctorPaths, platform: str | None = None) -> DoctorReport:
    """Run the doctor checks and (optionally) apply fixes.

    Args:
        fix: When true, apply the automatic repairs (migration / profile-dir /
            permissions) before computing the final health.
        paths: Injected path-resolver collaborators (see :class:`DoctorPaths`).
        platform: Override for :data:`sys.platform` (default). Only the
            profile-dir permission checks and fixes consult it — see
            :func:`_check_profile_dir`. Exists so the Windows carve-out is
            testable on any host instead of skipped off-Windows.

    Returns:
        A typed :class:`DoctorReport`. ``has_failures`` reflects the *final*
        check states (after any fixes), so a still-broken install reports a
        lingering failure for the adapter's non-zero exit.
    """
    path_info = paths.get_path_info()
    profile_name = path_info["profile"]
    profile_source = path_info["profile_source"]
    home = paths.get_home_dir()
    profile_dir = paths.get_profile_dir()

    checks: dict[str, dict[str, str]] = {
        "migration": _check_migration(home),
        "profile_dir": _check_profile_dir(profile_dir, platform=platform),
        "auth": _check_auth(paths.get_storage_path()),
        "config": _check_config(paths.get_config_path(), paths.get_profile_dir),
        "headless_reauth": paths.headless_reauth_check(),
    }

    fixes_applied: list[str] = []
    if fix:
        # Imported lazily (matches the historical CLI path) so the migration
        # machinery is only loaded when ``--fix`` is requested.
        from ..migration import migrate_to_profiles

        fixes_applied = _apply_fixes(
            checks, home, profile_dir, migrate_to_profiles, platform=platform
        )

    return DoctorReport(
        profile=profile_name,
        profile_source=profile_source,
        checks=checks,
        fixes_applied=fixes_applied,
    )


__all__ = [
    "DoctorPaths",
    "DoctorReport",
    "run_checks",
]
