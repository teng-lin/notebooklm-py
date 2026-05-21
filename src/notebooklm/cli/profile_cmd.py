"""Profile management CLI commands.

Commands:
    profile list      List all profiles
    profile create    Create a new profile
    profile switch    Set the default profile
    profile delete    Delete a profile
    profile rename    Rename a profile
"""

import json
import os
import shutil
import sys
from pathlib import Path

import click
from rich.table import Table

from ..auth import read_account_metadata
from ..io import atomic_update_json
from ..paths import (
    get_config_path,
    get_profile_dir,
    get_storage_path,
    list_profiles,
    read_default_profile,
    resolve_profile,
)
from .rendering import console, json_output_response
from .services import login as login_service

_PROFILE_NAME_RE = login_service._PROFILE_NAME_RE
_validate_profile_name = login_service._validate_profile_name
email_to_profile_name = login_service.email_to_profile_name


def _read_config(config_path: Path, *, suppress_errors: bool = True) -> dict:
    """Read global config, optionally tolerating unreadable/corrupt files."""
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        if not suppress_errors:
            raise
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_config(config_path: Path, mutator) -> None:
    """Lock + read-modify-write of the global config with private permissions.

    Wraps :func:`atomic_update_json` so parent dir permissions are 0o700 on
    POSIX (matching the legacy ``_write_config``). Use this for any code path
    that reads, mutates, and writes ``config.json`` so concurrent CLI
    invocations cannot lose updates.

    If the existing config is unparseable (corrupted on disk), the mutator
    runs on an empty dict instead — recovery happens **inside** the lock via
    ``recover_from_corrupt=True``. An outside-the-lock unlink-and-retry would
    race a concurrent process that wrote a valid payload between our raise
    and our retry, causing us to delete their good write (see PR #465).
    """
    if sys.platform == "win32":
        config_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path.parent.chmod(0o700)
    atomic_update_json(config_path, mutator, recover_from_corrupt=True)


@click.group("profile")
def profile():
    """Manage authentication profiles for multiple accounts."""
    pass


@profile.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def list_cmd(json_output):
    """List all profiles and their status."""
    profiles = list_profiles()
    active = resolve_profile()

    if not profiles:
        if json_output:
            json_output_response({"profiles": [], "active": active})
            return
        console.print("[yellow]No profiles found. Run 'notebooklm login' to create one.[/yellow]")
        return

    profile_data = []
    for name in profiles:
        storage = get_storage_path(profile=name)
        is_active = name == active
        authenticated = storage.exists()
        account_metadata = read_account_metadata(storage)
        account_email = account_metadata.get("email")

        profile_data.append(
            {
                "name": name,
                "active": is_active,
                "authenticated": authenticated,
                "account": account_email if isinstance(account_email, str) else None,
            }
        )

    if json_output:
        json_output_response({"profiles": profile_data, "active": active})
        return

    table = Table(title="Profiles")
    table.add_column("", width=2)
    table.add_column("Name", style="cyan")
    table.add_column("Account", style="dim")
    table.add_column("Auth Status")

    for p in profile_data:
        marker = "[green]*[/green]" if p["active"] else ""
        auth_status = (
            "[green]authenticated[/green]" if p["authenticated"] else "[dim]not authenticated[/dim]"
        )
        account = str(p["account"] or "-")
        table.add_row(marker, str(p["name"]), account, auth_status)

    console.print(table)
    console.print(f"\n[dim]Active profile: {active}[/dim]")


@profile.command("create")
@click.argument("name")
def create_cmd(name):
    """Create a new profile.

    Creates an empty profile directory. Use 'notebooklm -p NAME login' to authenticate.

    \b
    Example:
      notebooklm profile create work
      notebooklm -p work login
    """
    name = _validate_profile_name(name)

    try:
        profile_dir = get_profile_dir(name)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    if profile_dir.exists():
        raise click.ClickException(f"Profile '{name}' already exists.")

    get_profile_dir(name, create=True)
    console.print(f"[green]Profile '{name}' created.[/green]")
    console.print(f"[dim]Run 'notebooklm -p {name} login' to authenticate.[/dim]")


@profile.command("switch")
@click.argument("name")
def switch_cmd(name):
    """Set the default profile.

    \b
    Example:
      notebooklm profile switch work
      notebooklm list                   # Now uses 'work' profile
    """
    try:
        profile_dir = get_profile_dir(name)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    if not profile_dir.exists():
        available = list_profiles()
        hint = f" Available: {', '.join(available)}" if available else ""
        raise click.ClickException(f"Profile '{name}' not found.{hint}")

    config_path = get_config_path()
    # Capture the previous value for the status message before mutating.
    # The lock-protected mutator below is the source of truth for the write.
    old_profile = _read_config(config_path).get("default_profile", "default")

    def _set_default(data: dict) -> dict:
        data["default_profile"] = name
        return data

    try:
        _atomic_write_config(config_path, _set_default)
    except OSError as e:
        raise click.ClickException(f"Failed to update config.json: {e}") from None

    console.print(f"[green]Switched default profile: {old_profile} → {name}[/green]")


@profile.command("delete")
@click.argument("name")
@click.option("--confirm", is_flag=True, help="Skip confirmation prompt")
def delete_cmd(name, confirm):
    """Delete a profile and its data.

    Removes the profile directory including auth cookies, context, and browser profile.
    Cannot delete the currently active default profile.

    \b
    Example:
      notebooklm profile delete old-account --confirm
    """
    try:
        profile_dir = get_profile_dir(name)
    except ValueError as e:
        raise click.ClickException(str(e)) from None

    # Block deletion of active or configured default profile
    configured_default = read_default_profile() or "default"
    effective_active = resolve_profile()
    if name in (configured_default, effective_active):
        raise click.ClickException(
            f"Cannot delete active/default profile '{name}'. "
            f"Switch to another profile first with 'notebooklm profile switch <name>'."
        )

    if not profile_dir.exists():
        raise click.ClickException(f"Profile '{name}' not found.")

    if not confirm:
        if not click.confirm(f"Delete profile '{name}' and all its data?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    shutil.rmtree(profile_dir)
    console.print(f"[green]Profile '{name}' deleted.[/green]")


@profile.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def rename_cmd(old_name, new_name):
    """Rename a profile.

    \b
    Example:
      notebooklm profile rename work work-old
    """
    new_name = _validate_profile_name(new_name)

    try:
        old_dir = get_profile_dir(old_name)
        new_dir = get_profile_dir(new_name)
    except ValueError as e:
        raise click.ClickException(str(e)) from None

    if not old_dir.exists():
        raise click.ClickException(f"Profile '{old_name}' not found.")
    if new_dir.exists():
        raise click.ClickException(f"Profile '{new_name}' already exists.")

    os.rename(old_dir, new_dir)

    # Update config if renamed profile was the effective default. This is
    # always serialized through the locked mutator — there is NO pre-read
    # early-return optimization, because a concurrent ``profile switch``
    # could win between any pre-read and the lock acquire, leading us to
    # skip the write that was correct at the moment we observed it. The
    # mutator below is the single source of truth and recovers from a
    # corrupt config under the same lock (``recover_from_corrupt=True``
    # inside ``_atomic_write_config``).
    config_path = get_config_path()
    updated = False

    def _retarget_default(current: dict) -> dict:
        nonlocal updated
        # Decide under the lock — this is the only read of
        # ``default_profile`` that matters. Treat a missing key as the
        # implicit "default" so a fresh install with no config.json still
        # picks up the rename when the user renamed the default profile.
        if (current.get("default_profile") or "default") == old_name:
            current["default_profile"] = new_name
            updated = True
        return current

    try:
        _atomic_write_config(config_path, _retarget_default)
    except OSError as e:
        console.print(
            f"[yellow]Warning: profile renamed but config.json update failed: {e}[/yellow]\n"
            f"[yellow]Run 'notebooklm profile switch {new_name}' to fix.[/yellow]"
        )
    else:
        if updated:
            console.print(f"[dim]Updated default profile in config: {old_name} → {new_name}[/dim]")

    console.print(f"[green]Profile renamed: {old_name} → {new_name}[/green]")
