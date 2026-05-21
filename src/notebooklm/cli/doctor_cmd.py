"""Diagnostic and migration CLI command.

Commands:
    doctor   Check profile setup, auth, and migration status
"""

import json
import logging

import click
from rich.table import Table

from ..paths import (
    get_config_path,
    get_home_dir,
    get_path_info,
    get_profile_dir,
    get_storage_path,
)
from .rendering import console, json_output_response

logger = logging.getLogger(__name__)


def register_doctor_command(cli):
    """Register the doctor command on the main CLI group."""

    @cli.command("doctor")
    @click.option("--fix", "fix_issues", is_flag=True, help="Attempt to fix detected issues")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def doctor(fix_issues, json_output):
        """Check profile setup, auth status, and migration.

        Diagnoses common issues with profiles, authentication, and directory
        structure. Use --fix to automatically repair detected problems.

        \b
        Examples:
          notebooklm doctor           # Check for issues
          notebooklm doctor --fix     # Fix detected issues
          notebooklm doctor --json    # Machine-readable output
        """
        checks: dict[str, dict] = {}
        path_info = get_path_info()
        profile_name = path_info["profile"]
        profile_source = path_info["profile_source"]
        home = get_home_dir()

        # Check 1: Migration status
        profiles_dir = home / "profiles"
        has_legacy = any(
            (home / name).exists()
            for name in ("storage_state.json", "context.json", "browser_profile")
        )
        has_profiles = profiles_dir.exists()

        if has_profiles and not has_legacy:
            checks["migration"] = {"status": "pass", "detail": "complete"}
        elif has_legacy and not has_profiles:
            checks["migration"] = {"status": "fail", "detail": "legacy layout detected"}
        elif has_legacy and has_profiles:
            checks["migration"] = {
                "status": "warn",
                "detail": "legacy files remain alongside profiles",
            }
        else:
            checks["migration"] = {"status": "pass", "detail": "clean (no legacy files)"}

        # Check 2: Profile directory
        profile_dir = get_profile_dir()
        if profile_dir.exists():
            perms = profile_dir.stat().st_mode & 0o777
            if perms == 0o700:
                checks["profile_dir"] = {"status": "pass", "detail": str(profile_dir)}
            else:
                checks["profile_dir"] = {
                    "status": "warn",
                    "detail": f"{profile_dir} (permissions: {oct(perms)}, expected: 0o700)",
                }
        else:
            checks["profile_dir"] = {
                "status": "fail",
                "detail": f"{profile_dir} not found",
            }

        # Check 3: Auth
        storage_path = get_storage_path()
        if storage_path.exists():
            try:
                data = json.loads(storage_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("storage root is not an object")
                cookies = data.get("cookies", [])
                if not isinstance(cookies, list):
                    raise ValueError("cookies is not a list")
                cookie_names = {c.get("name") for c in cookies if isinstance(c, dict)}
                if "SID" in cookie_names:
                    checks["auth"] = {
                        "status": "pass",
                        "detail": f"local SID cookie present ({len(cookie_names)} cookies)",
                    }
                else:
                    checks["auth"] = {
                        "status": "fail",
                        "detail": "SID cookie missing",
                    }
            except (json.JSONDecodeError, OSError, ValueError) as e:
                checks["auth"] = {"status": "fail", "detail": f"invalid storage file: {e}"}
        else:
            checks["auth"] = {"status": "fail", "detail": "not authenticated"}

        # Check 4: Config
        config_path = get_config_path()
        if config_path.exists():
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
                        checks["config"] = {
                            "status": "pass",
                            "detail": f"valid (default_profile: {default_profile})",
                        }
                    else:
                        checks["config"] = {
                            "status": "warn",
                            "detail": f"default_profile '{default_profile}' does not exist",
                        }
                else:
                    checks["config"] = {
                        "status": "pass",
                        "detail": "valid (no default_profile set)",
                    }
            except (json.JSONDecodeError, OSError, ValueError) as e:
                checks["config"] = {"status": "fail", "detail": f"invalid: {e}"}
        else:
            checks["config"] = {"status": "pass", "detail": "not present (using defaults)"}

        # Apply fixes if requested
        fixes_applied = []
        if fix_issues:
            fixes_applied = _apply_fixes(checks, home, profile_dir)

        # Output
        if json_output:
            result = {
                "profile": profile_name,
                "profile_source": profile_source,
                "checks": checks,
            }
            if fixes_applied:
                result["fixes_applied"] = fixes_applied
            json_output_response(result)
            return

        _display_results(profile_name, profile_source, checks, fixes_applied)


def _apply_fixes(checks: dict, home, profile_dir) -> list[str]:
    """Apply automatic fixes for detected issues."""
    fixes = []

    # Fix migration (both "fail" = no profiles dir, and "warn" = partial migration)
    if checks["migration"]["status"] in ("fail", "warn"):
        from ..migration import migrate_to_profiles

        if migrate_to_profiles():
            fixes.append("Migrated legacy layout to profiles/default/")
            checks["migration"] = {"status": "pass", "detail": "complete (just migrated)"}
            if profile_dir.exists():
                checks["profile_dir"] = {"status": "pass", "detail": str(profile_dir)}

    # Fix missing profile directory
    if checks["profile_dir"]["status"] == "fail":
        profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fixes.append(f"Created profile directory: {profile_dir}")
        checks["profile_dir"] = {"status": "pass", "detail": str(profile_dir)}

    # Fix permissions
    if (
        checks["profile_dir"]["status"] == "warn"
        and "permissions" in checks["profile_dir"]["detail"]
    ):
        profile_dir.chmod(0o700)
        fixes.append(f"Fixed permissions on {profile_dir}")
        checks["profile_dir"] = {"status": "pass", "detail": str(profile_dir)}

    return fixes


def _display_results(
    profile_name: str, profile_source: str, checks: dict, fixes_applied: list[str]
):
    """Display doctor results using Rich."""
    table = Table(title="NotebookLM Doctor")
    table.add_column("Check", style="dim")
    table.add_column("Status")
    table.add_column("Details", style="cyan")

    def status_icon(status: str) -> str:
        if status == "pass":
            return "[green]\u2713 pass[/green]"
        elif status == "warn":
            return "[yellow]! warn[/yellow]"
        return "[red]\u2717 fail[/red]"

    table.add_row("Profile", f"[bold]{profile_name}[/bold]", f"source: {profile_source}")

    for name, check in checks.items():
        label = name.replace("_", " ").title()
        table.add_row(label, status_icon(check["status"]), check["detail"])

    console.print(table)

    if fixes_applied:
        console.print()
        for fix in fixes_applied:
            console.print(f"  [green]\u2713[/green] {fix}")

    has_failures = any(c["status"] == "fail" for c in checks.values())
    if has_failures and not fixes_applied:
        console.print()
        if checks.get("migration", {}).get("status") == "fail":
            console.print(
                "[yellow]Run 'notebooklm doctor --fix' to migrate and set up profiles.[/yellow]"
            )
        if checks.get("auth", {}).get("status") == "fail":
            console.print("[yellow]Run 'notebooklm login' to authenticate.[/yellow]")
        if checks.get("profile_dir", {}).get("status") == "fail":
            console.print(
                "[yellow]Run 'notebooklm doctor --fix' to create the profile directory.[/yellow]"
            )
    elif not has_failures:
        console.print("\n[green]All checks passed.[/green]")
