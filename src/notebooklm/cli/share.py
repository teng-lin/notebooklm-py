"""Sharing management CLI commands.

Commands:
    status       Show sharing status and shared users
    public       Enable or disable public link sharing
    view-level   Set what viewers can access
    add          Share with a user
    update       Update user's permission
    remove       Remove user's access
"""

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import SharePermission, ShareViewLevel
from .helpers import (
    console,
    json_output_response,
    require_notebook,
    resolve_notebook_id,
    should_confirm,
    with_client,
)
from .options import json_option


def _permission_name(perm: SharePermission) -> str:
    """Convert permission enum to display name."""
    return {
        SharePermission.OWNER: "Owner",
        SharePermission.EDITOR: "Editor",
        SharePermission.VIEWER: "Viewer",
    }.get(perm, "Unknown")


def _view_level_display(view_level: ShareViewLevel) -> str:
    """Convert view level enum to display name."""
    if view_level == ShareViewLevel.FULL_NOTEBOOK:
        return "Full Notebook"
    return "Chat Only"


def _parse_permission(permission: str) -> SharePermission:
    """Parse permission string to enum."""
    if permission.lower() == "editor":
        return SharePermission.EDITOR
    return SharePermission.VIEWER


@click.group()
def share():
    """Notebook sharing commands.

    \b
    Commands:
      status       Show sharing status and shared users
      public       Enable or disable public link sharing
      view-level   Set what viewers can access (full notebook or chat only)
      add          Share with a user (editor or viewer)
      update       Update user's permission level
      remove       Remove user's access

    \b
    Examples:
      notebooklm share status              # Show current sharing
      notebooklm share public --enable     # Make notebook public
      notebooklm share add user@example.com --permission viewer
      notebooklm share remove user@example.com
    """
    pass


@share.command("status")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@json_option
@with_client
def share_status(ctx, notebook_id, json_output, client_auth):
    """Show sharing status and shared users.

    Displays whether the notebook is public, the share URL if enabled,
    and a list of users who have access with their permission levels.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_notebook_id(client, nb_id)
            status = await client.sharing.get_status(resolved_id)

            if json_output:
                data = {
                    "notebook_id": status.notebook_id,
                    "is_public": status.is_public,
                    "access": status.access.name.lower(),
                    "view_level": status.view_level.name.lower(),
                    "share_url": status.share_url,
                    "shared_users": [
                        {
                            "email": u.email,
                            "permission": u.permission.name.lower(),
                            "display_name": u.display_name,
                        }
                        for u in status.shared_users
                    ],
                }
                json_output_response(data)
                return

            # Display status
            access_status = (
                "[green]Public[/green]" if status.is_public else "[yellow]Private[/yellow]"
            )
            console.print(f"[bold]Sharing Status:[/bold] {access_status}")

            if status.share_url:
                console.print(f"[bold]Share URL:[/bold] [blue]{status.share_url}[/blue]")

            console.print(
                f"[bold]View Level:[/bold] {_view_level_display(status.view_level)} "
                "[dim](use 'share view-level' to change)[/dim]"
            )

            # Display shared users
            if status.shared_users:
                console.print()
                table = Table(title="Shared Users")
                table.add_column("Email", style="cyan")
                table.add_column("Name")
                table.add_column("Permission", style="green")

                for user in status.shared_users:
                    name = user.display_name or "-"
                    perm = _permission_name(user.permission)
                    table.add_row(user.email, name, perm)

                console.print(table)
            else:
                console.print("\n[dim]No users shared with this notebook[/dim]")

    return _run()


@share.command("public")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@click.option("--enable/--disable", default=True, help="Enable or disable public sharing")
@json_option
@with_client
def share_public(ctx, notebook_id, enable, json_output, client_auth):
    """Enable or disable public link sharing.

    When public sharing is enabled, anyone with the link can view
    the notebook (read-only access).

    \b
    Examples:
      notebooklm share public              # Enable (default)
      notebooklm share public --enable     # Enable explicitly
      notebooklm share public --disable    # Disable
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_notebook_id(client, nb_id)
            status = await client.sharing.set_public(resolved_id, enable)

            if json_output:
                data = {
                    "notebook_id": status.notebook_id,
                    "is_public": status.is_public,
                    "share_url": status.share_url,
                }
                json_output_response(data)
                return

            if status.is_public:
                console.print("[green]Public sharing enabled[/green]")
                if status.share_url:
                    console.print(f"[bold]Share URL:[/bold] [blue]{status.share_url}[/blue]")
            else:
                console.print("[yellow]Public sharing disabled[/yellow]")

    return _run()


@share.command("view-level")
@click.argument("level", type=click.Choice(["full", "chat"], case_sensitive=False))
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@json_option
@with_client
def share_view_level(ctx, level, notebook_id, json_output, client_auth):
    """Set what viewers can access.

    \b
    LEVEL options:
      full   - Viewers see chat, sources, and notes
      chat   - Viewers only see the chat interface

    \b
    Examples:
      notebooklm share view-level full     # Full notebook access
      notebooklm share view-level chat     # Chat only
    """
    nb_id = require_notebook(notebook_id)
    view_level = (
        ShareViewLevel.FULL_NOTEBOOK if level.lower() == "full" else ShareViewLevel.CHAT_ONLY
    )

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_notebook_id(client, nb_id)
            status = await client.sharing.set_view_level(resolved_id, view_level)

            if json_output:
                data = {
                    "notebook_id": resolved_id,
                    "view_level": status.view_level.name.lower(),
                }
                json_output_response(data)
                return

            console.print(
                f"[green]View level set to:[/green] {_view_level_display(status.view_level)}"
            )

    return _run()


@share.command("add")
@click.argument("email")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@click.option(
    "--permission",
    "-p",
    type=click.Choice(["editor", "viewer"], case_sensitive=False),
    default="viewer",
    help="Permission level (default: viewer)",
)
@click.option("--no-notify", is_flag=True, help="Don't send email notification")
@click.option("--message", "-m", default="", help="Welcome message for the user")
@json_option
@with_client
def share_add(ctx, email, notebook_id, permission, no_notify, message, json_output, client_auth):
    """Share notebook with a user.

    Adds a user with the specified permission level. By default, sends
    an email notification to the user.

    \b
    Examples:
      notebooklm share add user@example.com
      notebooklm share add user@example.com --permission editor
      notebooklm share add user@example.com -m "Check out my research!"
      notebooklm share add user@example.com --no-notify
    """
    nb_id = require_notebook(notebook_id)
    perm = _parse_permission(permission)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_notebook_id(client, nb_id)
            await client.sharing.add_user(
                resolved_id,
                email,
                permission=perm,
                notify=not no_notify,
                welcome_message=message,
            )

            if json_output:
                data = {
                    "notebook_id": resolved_id,
                    "added_user": email,
                    "permission": permission.lower(),
                    "notified": not no_notify,
                }
                json_output_response(data)
                return

            console.print(f"[green]Shared with {email}[/green] as {_permission_name(perm)}")
            if not no_notify:
                console.print("[dim]Email notification sent[/dim]")

    return _run()


@share.command("update")
@click.argument("email")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@click.option(
    "--permission",
    "-p",
    type=click.Choice(["editor", "viewer"], case_sensitive=False),
    required=True,
    help="New permission level",
)
@json_option
@with_client
def share_update(ctx, email, notebook_id, permission, json_output, client_auth):
    """Update a user's permission level.

    Changes the permission level for a user who already has access.

    \b
    Examples:
      notebooklm share update user@example.com --permission editor
      notebooklm share update user@example.com -p viewer
    """
    nb_id = require_notebook(notebook_id)
    perm = _parse_permission(permission)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_notebook_id(client, nb_id)
            await client.sharing.update_user(resolved_id, email, perm)

            if json_output:
                data = {
                    "notebook_id": resolved_id,
                    "updated_user": email,
                    "permission": permission.lower(),
                }
                json_output_response(data)
                return

            console.print(f"[green]Updated {email}[/green] to {_permission_name(perm)}")

    return _run()


@share.command("remove")
@click.argument("email")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def share_remove(ctx, email, notebook_id, yes, json_output, client_auth):
    """Remove a user's access to the notebook.

    \b
    Examples:
      notebooklm share remove user@example.com
      notebooklm share remove user@example.com -y  # Skip confirmation
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_notebook_id(client, nb_id)

            # Confirm after resolution so user sees context
            if should_confirm(yes, json_output) and not click.confirm(
                f"Remove access for {email}?"
            ):
                return

            await client.sharing.remove_user(resolved_id, email)

            if json_output:
                data = {
                    "notebook_id": resolved_id,
                    "removed_user": email,
                }
                json_output_response(data)
                return

            console.print(f"[green]Removed access for {email}[/green]")

    return _run()
