"""Session and context management CLI commands.

Commands:
    login   Log in to NotebookLM via browser
    use     Set the current notebook context
    status  Show current context
    clear   Clear current notebook context
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from ..auth import AuthTokens
from ..client import NotebookLMClient
from ..paths import (
    get_browser_profile_dir,
    get_context_path,
    get_path_info,
    get_storage_path,
)
from .helpers import (
    clear_context,
    console,
    get_client,
    get_current_notebook,
    json_output_response,
    resolve_notebook_id,
    run_async,
    set_current_notebook,
)


def _ensure_chromium_installed() -> None:
    """Check if Chromium is installed and install if needed.

    This pre-flight check runs `playwright install --dry-run chromium` to detect
    if the browser needs installation, then auto-installs if necessary.

    Silently proceeds on any errors - Playwright will handle them during launch.
    """
    try:
        result = subprocess.run(
            ["playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
        )
        # Check if dry-run indicates browser needs installing
        stdout_lower = result.stdout.lower()
        if "chromium" not in stdout_lower or "will download" not in stdout_lower:
            return

        console.print("[yellow]Chromium browser not installed. Installing now...[/yellow]")
        install_result = subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if install_result.returncode != 0:
            console.print(
                "[red]Failed to install Chromium browser.[/red]\n"
                "Run manually: playwright install chromium"
            )
            raise SystemExit(1)
        console.print("[green]Chromium installed successfully.[/green]\n")
    except SystemExit:
        raise
    except Exception as e:
        # FileNotFoundError: playwright CLI not found but sync_playwright imported
        # Other exceptions: dry-run check failed - let Playwright handle it during launch
        console.print(
            f"[dim]Warning: Chromium pre-flight check failed: {e}. Proceeding anyway.[/dim]"
        )


def register_session_commands(cli):
    """Register session commands on the main CLI group."""

    @cli.command("login")
    @click.option(
        "--storage",
        type=click.Path(),
        default=None,
        help="Where to save storage_state.json (default: $NOTEBOOKLM_HOME/storage_state.json)",
    )
    def login(storage):
        """Log in to NotebookLM via browser.

        Opens a browser window for Google login. After logging in,
        press ENTER in the terminal to save authentication.

        Note: Cannot be used when NOTEBOOKLM_AUTH_JSON is set (use file-based
        auth or unset the env var first).
        """
        # Check for conflicting env var
        if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            console.print(
                "[red]Error: Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set.[/red]\n"
                "The NOTEBOOKLM_AUTH_JSON environment variable provides inline authentication,\n"
                "which conflicts with browser-based login that saves to a file.\n\n"
                "Either:\n"
                "  1. Unset NOTEBOOKLM_AUTH_JSON and run 'login' again\n"
                "  2. Continue using NOTEBOOKLM_AUTH_JSON for authentication"
            )
            raise SystemExit(1)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            console.print(
                "[red]Playwright not installed. Run:[/red]\n"
                "  pip install notebooklm[browser]\n"
                "  playwright install chromium"
            )
            raise SystemExit(1) from None

        # Pre-flight check: verify Chromium browser is installed
        _ensure_chromium_installed()

        storage_path = Path(storage) if storage else get_storage_path()
        browser_profile = get_browser_profile_dir()
        storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        browser_profile.mkdir(parents=True, exist_ok=True, mode=0o700)

        console.print("[yellow]Opening browser for Google login...[/yellow]")
        console.print(f"[dim]Using persistent profile: {browser_profile}[/dim]")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(browser_profile),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--password-store=basic",  # Avoid macOS keychain encryption for headless compatibility
                ],
                ignore_default_args=["--enable-automation"],
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://notebooklm.google.com/")

            console.print("\n[bold green]Instructions:[/bold green]")
            console.print("1. Complete the Google login in the browser window")
            console.print("2. Wait until you see the NotebookLM homepage")
            console.print("3. Press [bold]ENTER[/bold] here to save and close\n")

            input("[Press ENTER when logged in] ")

            current_url = page.url
            if "notebooklm.google.com" not in current_url:
                console.print(f"[yellow]Warning: Current URL is {current_url}[/yellow]")
                if not click.confirm("Save authentication anyway?"):
                    context.close()
                    raise SystemExit(1)

            context.storage_state(path=str(storage_path))
            # Restrict permissions to owner only (contains sensitive cookies)
            storage_path.chmod(0o600)
            context.close()

        console.print(f"\n[green]Authentication saved to:[/green] {storage_path}")

    @cli.command("use")
    @click.argument("notebook_id")
    @click.pass_context
    def use_notebook(ctx, notebook_id):
        """Set the current notebook context.

        Once set, all commands will use this notebook by default.
        You can still override by passing --notebook explicitly.

        Supports partial IDs - 'notebooklm use abc' matches 'abc123...'

        \b
        Example:
          notebooklm use nb123
          notebooklm ask "what is this about?"   # Uses nb123
          notebooklm generate video "a fun explainer"  # Uses nb123
        """
        try:
            cookies, csrf, session_id = get_client(ctx)
            auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

            async def _get():
                async with NotebookLMClient(auth) as client:
                    # Resolve partial ID to full ID
                    resolved_id = await resolve_notebook_id(client, notebook_id)
                    nb = await client.notebooks.get(resolved_id)
                    return nb, resolved_id

            nb, resolved_id = run_async(_get())

            created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
            set_current_notebook(resolved_id, nb.title, nb.is_owner, created_str)

            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")

            created = created_str or "-"
            owner_status = "Owner" if nb.is_owner else "Shared"
            table.add_row(nb.id, nb.title, owner_status, created)

            console.print(table)

        except FileNotFoundError:
            set_current_notebook(notebook_id)
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")
            table.add_row(notebook_id, "-", "-", "-")
            console.print(table)
        except click.ClickException:
            # Re-raise click exceptions (from resolve_notebook_id)
            raise
        except Exception as e:
            set_current_notebook(notebook_id)
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")
            table.add_row(notebook_id, f"Warning: {str(e)}", "-", "-")
            console.print(table)

    @cli.command("status")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option("--paths", "show_paths", is_flag=True, help="Show resolved file paths")
    def status(json_output, show_paths):
        """Show current context (active notebook and conversation).

        Use --paths to see where configuration files are located
        (useful for debugging NOTEBOOKLM_HOME).
        """
        context_file = get_context_path()
        notebook_id = get_current_notebook()

        # Handle --paths flag
        if show_paths:
            path_info = get_path_info()
            if json_output:
                json_output_response({"paths": path_info})
                return

            table = Table(title="Configuration Paths")
            table.add_column("File", style="dim")
            table.add_column("Path", style="cyan")
            table.add_column("Source", style="green")

            table.add_row("Home Directory", path_info["home_dir"], path_info["home_source"])
            table.add_row("Storage State", path_info["storage_path"], "")
            table.add_row("Context", path_info["context_path"], "")
            table.add_row("Browser Profile", path_info["browser_profile_dir"], "")

            # Show if NOTEBOOKLM_AUTH_JSON is set
            if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
                console.print(
                    "[yellow]Note: NOTEBOOKLM_AUTH_JSON is set (inline auth active)[/yellow]\n"
                )

            console.print(table)
            return

        if notebook_id:
            try:
                data = json.loads(context_file.read_text())
                title = data.get("title", "-")
                is_owner = data.get("is_owner", True)
                created_at = data.get("created_at", "-")
                conversation_id = data.get("conversation_id")

                if json_output:
                    json_data = {
                        "has_context": True,
                        "notebook": {
                            "id": notebook_id,
                            "title": title if title != "-" else None,
                            "is_owner": is_owner,
                        },
                        "conversation_id": conversation_id,
                    }
                    json_output_response(json_data)
                    return

                table = Table(title="Current Context")
                table.add_column("Property", style="dim")
                table.add_column("Value", style="cyan")

                table.add_row("Notebook ID", notebook_id)
                table.add_row("Title", str(title))
                owner_status = "Owner" if is_owner else "Shared"
                table.add_row("Ownership", owner_status)
                table.add_row("Created", created_at)
                if conversation_id:
                    table.add_row("Conversation", conversation_id)
                else:
                    table.add_row("Conversation", "[dim]None (will auto-select on next ask)[/dim]")
                console.print(table)
            except (OSError, json.JSONDecodeError):
                if json_output:
                    json_data = {
                        "has_context": True,
                        "notebook": {
                            "id": notebook_id,
                            "title": None,
                            "is_owner": None,
                        },
                        "conversation_id": None,
                    }
                    json_output_response(json_data)
                    return

                table = Table(title="Current Context")
                table.add_column("Property", style="dim")
                table.add_column("Value", style="cyan")
                table.add_row("Notebook ID", notebook_id)
                table.add_row("Title", "-")
                table.add_row("Ownership", "-")
                table.add_row("Created", "-")
                table.add_row("Conversation", "[dim]None[/dim]")
                console.print(table)
        else:
            if json_output:
                json_data = {
                    "has_context": False,
                    "notebook": None,
                    "conversation_id": None,
                }
                json_output_response(json_data)
                return

            console.print(
                "[yellow]No notebook selected. Use 'notebooklm use <id>' to set one.[/yellow]"
            )

    @cli.command("clear")
    def clear_cmd():
        """Clear current notebook context."""
        clear_context()
        console.print("[green]Context cleared[/green]")

    @cli.group("auth")
    def auth_group():
        """Authentication management commands."""
        pass

    @auth_group.command("check")
    @click.option(
        "--test", "test_fetch", is_flag=True, help="Test token fetch (makes network request)"
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def auth_check(test_fetch, json_output):
        """Check authentication status and diagnose issues.

        Validates that authentication is properly configured by checking:
        - Storage file exists and is readable
        - JSON structure is valid
        - Required cookies (SID) are present
        - Cookie domains are correct

        Use --test to also verify tokens can be fetched from NotebookLM
        (requires network access).

        \b
        Examples:
          notebooklm auth check           # Quick local validation
          notebooklm auth check --test    # Full validation with network test
          notebooklm auth check --json    # Machine-readable output
        """
        from ..auth import (
            extract_cookies_from_storage,
            fetch_tokens,
        )

        storage_path = get_storage_path()
        has_env_var = bool(os.environ.get("NOTEBOOKLM_AUTH_JSON"))
        has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))

        checks: dict[str, bool | None] = {
            "storage_exists": False,
            "json_valid": False,
            "cookies_present": False,
            "sid_cookie": False,
            "token_fetch": None,  # None = not tested, True/False = result
        }

        # Determine auth source for display
        if has_env_var:
            auth_source = "NOTEBOOKLM_AUTH_JSON"
        elif has_home_env:
            auth_source = f"$NOTEBOOKLM_HOME ({storage_path})"
        else:
            auth_source = f"file ({storage_path})"

        details: dict[str, Any] = {
            "storage_path": str(storage_path),
            "auth_source": auth_source,
            "cookies_found": [],
            "cookie_domains": [],
            "error": None,
        }

        # Check 1: Storage exists
        if has_env_var:
            checks["storage_exists"] = True
        else:
            checks["storage_exists"] = storage_path.exists()

        if not checks["storage_exists"]:
            details["error"] = f"Storage file not found: {storage_path}"
            _output_auth_check(checks, details, json_output)
            return

        # Check 2: JSON valid
        try:
            if has_env_var:
                storage_state = json.loads(os.environ["NOTEBOOKLM_AUTH_JSON"])
            else:
                storage_state = json.loads(storage_path.read_text())
            checks["json_valid"] = True
        except json.JSONDecodeError as e:
            details["error"] = f"Invalid JSON: {e}"
            _output_auth_check(checks, details, json_output)
            return

        # Check 3: Cookies present
        try:
            cookies = extract_cookies_from_storage(storage_state)
            checks["cookies_present"] = True
            checks["sid_cookie"] = "SID" in cookies
            details["cookies_found"] = list(cookies.keys())

            # Build detailed cookie-by-domain mapping for debugging
            cookies_by_domain: dict[str, list[str]] = {}
            for cookie in storage_state.get("cookies", []):
                domain = cookie.get("domain", "")
                name = cookie.get("name", "")
                if domain and name and "google" in domain.lower():
                    cookies_by_domain.setdefault(domain, []).append(name)

            details["cookies_by_domain"] = cookies_by_domain
            details["cookie_domains"] = sorted(cookies_by_domain.keys())
        except ValueError as e:
            details["error"] = str(e)
            _output_auth_check(checks, details, json_output)
            return

        # Check 4: Token fetch (optional)
        if test_fetch:
            try:
                csrf, session_id = run_async(fetch_tokens(cookies))
                checks["token_fetch"] = True
                details["csrf_length"] = len(csrf)
                details["session_id_length"] = len(session_id)
            except Exception as e:
                checks["token_fetch"] = False
                details["error"] = f"Token fetch failed: {e}"

        _output_auth_check(checks, details, json_output)

    def _output_auth_check(checks: dict, details: dict, json_output: bool):
        """Output auth check results."""
        all_passed = all(v is True for v in checks.values() if v is not None)

        if json_output:
            json_output_response(
                {
                    "status": "ok" if all_passed else "error",
                    "checks": checks,
                    "details": details,
                }
            )
            return

        # Rich output
        table = Table(title="Authentication Check")
        table.add_column("Check", style="dim")
        table.add_column("Status")
        table.add_column("Details", style="cyan")

        def status_icon(val):
            if val is None:
                return "[dim]⊘ skipped[/dim]"
            return "[green]✓ pass[/green]" if val else "[red]✗ fail[/red]"

        table.add_row(
            "Storage exists",
            status_icon(checks["storage_exists"]),
            details["auth_source"],
        )
        table.add_row(
            "JSON valid",
            status_icon(checks["json_valid"]),
            "",
        )
        table.add_row(
            "Cookies present",
            status_icon(checks["cookies_present"]),
            f"{len(details.get('cookies_found', []))} cookies" if checks["cookies_present"] else "",
        )
        table.add_row(
            "SID cookie",
            status_icon(checks["sid_cookie"]),
            ", ".join(details.get("cookie_domains", [])[:3]) or "",
        )
        table.add_row(
            "Token fetch",
            status_icon(checks["token_fetch"]),
            "use --test to check" if checks["token_fetch"] is None else "",
        )

        console.print(table)

        # Show detailed cookie breakdown by domain
        cookies_by_domain = details.get("cookies_by_domain", {})
        if cookies_by_domain:
            console.print()  # Blank line
            cookie_table = Table(title="Cookies by Domain")
            cookie_table.add_column("Domain", style="cyan")
            cookie_table.add_column("Cookies")

            # Key auth cookies to highlight
            key_cookies = {"SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC"}

            def format_cookie_name(name: str) -> str:
                if name in key_cookies:
                    return f"[green]{name}[/green]"
                if name.startswith("__Secure-"):
                    return f"[blue]{name}[/blue]"
                return f"[dim]{name}[/dim]"

            for domain in sorted(cookies_by_domain.keys()):
                cookie_names = cookies_by_domain[domain]
                formatted = [format_cookie_name(name) for name in sorted(cookie_names)]
                cookie_table.add_row(domain, ", ".join(formatted))

            console.print(cookie_table)

        if details.get("error"):
            console.print(f"\n[red]Error:[/red] {details['error']}")

        if all_passed:
            console.print("\n[green]Authentication is valid.[/green]")
        elif not checks["storage_exists"]:
            console.print("\n[yellow]Run 'notebooklm login' to authenticate.[/yellow]")
        elif checks["token_fetch"] is False:
            console.print(
                "\n[yellow]Cookies may be expired. Run 'notebooklm login' to refresh.[/yellow]"
            )
