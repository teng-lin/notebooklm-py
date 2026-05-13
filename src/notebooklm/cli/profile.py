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
import re
import shutil

import click
from rich.table import Table

from ..auth import read_account_metadata
from ..paths import (
    get_config_path,
    get_profile_dir,
    get_storage_path,
    list_profiles,
    resolve_profile,
)
from .helpers import console, json_output_response

# Profile name validation: alphanumeric, hyphens, underscores. Must start with alphanum.
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_profile_name(name: str) -> str:
    """Validate a profile name."""
    if not _PROFILE_NAME_RE.match(name):
        raise click.ClickException(
            f"Invalid profile name '{name}'. "
            "Use alphanumeric characters, hyphens, and underscores. Must start with a letter or digit."
        )
    return name


def email_to_profile_name(email: str, *, fallback: str = "account") -> str:
    """Derive a valid profile name from an email address.

    Profile names are restricted to ``[a-zA-Z0-9_-]`` (see
    :data:`_PROFILE_NAME_RE`) and must start with an alphanumeric character.
    Email local-parts routinely contain ``.``, ``+``, etc. that aren't
    allowed, so we rewrite them to hyphens.

    Examples::

        alice@example.com         -> "alice"
        alice.smith@example.com   -> "alice-smith"
        bob+work@gmail.com        -> "bob-work"
        teng.lin.9414@gmail.com   -> "teng-lin-9414"

    Args:
        email: Account email address.
        fallback: Profile name to use when sanitization yields an empty
            string or a name that does not start with an alphanum.

    Returns:
        A profile name guaranteed to satisfy :data:`_PROFILE_NAME_RE`.
    """
    local = email.split("@", 1)[0] if "@" in email else email
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "-", local)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-_")
    if not sanitized or not _PROFILE_NAME_RE.match(sanitized):
        # The function's contract is "always returns a valid profile name", so
        # protect callers that pass a malformed fallback (e.g. "-tmp").
        return fallback if _PROFILE_NAME_RE.match(fallback) else "account"
    return sanitized


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
    data: dict = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    old_profile = data.get("default_profile", "default")
    data["default_profile"] = name
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

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
    from ..paths import _read_default_profile

    configured_default = _read_default_profile() or "default"
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

    # Update config if renamed profile was the effective default.
    # This handles both: config.json exists with default_profile=old_name,
    # AND config.json doesn't exist (implicit "default" fallback).
    config_path = get_config_path()
    try:
        data: dict = {}
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
        configured_default = data.get("default_profile") or "default"
        if configured_default == old_name:
            data["default_profile"] = new_name
            config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            config_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            console.print(f"[dim]Updated default profile in config: {old_name} → {new_name}[/dim]")
    except (json.JSONDecodeError, OSError) as e:
        console.print(
            f"[yellow]Warning: profile renamed but config.json update failed: {e}[/yellow]\n"
            f"[yellow]Run 'notebooklm profile switch {new_name}' to fix.[/yellow]"
        )

    console.print(f"[green]Profile renamed: {old_name} → {new_name}[/green]")
