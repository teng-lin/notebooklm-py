# Configuration

**Status:** Active
**Last Updated:** 2026-01-08

This guide covers storage locations, environment settings, and configuration options for `notebooklm-py`.

## File Locations

All data is stored under `~/.notebooklm/` by default:

```
~/.notebooklm/
├── storage_state.json    # Authentication cookies and session
├── context.json          # CLI context (active notebook, conversation)
└── browser_profile/      # Persistent Chromium profile
```

You can relocate all files by setting `NOTEBOOKLM_HOME`:

```bash
export NOTEBOOKLM_HOME=/custom/path
# All files now go to /custom/path/
```

### Storage State (`storage_state.json`)

Contains the authentication data extracted from your browser session:

```json
{
  "cookies": [
    {
      "name": "SID",
      "value": "...",
      "domain": ".google.com",
      "path": "/",
      "expires": 1234567890,
      "httpOnly": true,
      "secure": true,
      "sameSite": "Lax"
    },
    ...
  ],
  "origins": []
}
```

**Required cookies:** `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`, `__Secure-3PSID`

**Override location:**
```bash
notebooklm --storage /path/to/storage_state.json list
```

### Context File (`context.json`)

Stores the current CLI context (active notebook and conversation):

```json
{
  "notebook_id": "abc123def456",
  "conversation_id": "conv789"
}
```

This file is managed automatically by `notebooklm use` and `notebooklm clear`.

### Browser Profile (`browser_profile/`)

A persistent Chromium user data directory used during `notebooklm login`.

**Why persistent?** Google blocks automated login attempts. A persistent profile makes the browser appear as a regular user installation, avoiding bot detection.

**To reset:** Delete the `browser_profile/` directory and run `notebooklm login` again.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NOTEBOOKLM_HOME` | Base directory for all files | `~/.notebooklm` |
| `NOTEBOOKLM_AUTH_JSON` | Inline authentication JSON (for CI/CD) | - |
| `NOTEBOOKLM_DEBUG_RPC` | Enable RPC debug logging | `false` |

### NOTEBOOKLM_HOME

Relocates all configuration files to a custom directory:

```bash
export NOTEBOOKLM_HOME=/custom/path

# All files now go here:
# /custom/path/storage_state.json
# /custom/path/context.json
# /custom/path/browser_profile/
```

**Use cases:**
- Multiple Google accounts (separate home directories)
- Per-project isolation
- Custom storage locations

### NOTEBOOKLM_AUTH_JSON

Provides authentication inline without writing files. Ideal for CI/CD:

```bash
export NOTEBOOKLM_AUTH_JSON='{"cookies":[...]}'
notebooklm list  # Works without any file on disk
```

**Precedence:**
1. `--storage` CLI flag (highest)
2. `NOTEBOOKLM_AUTH_JSON` environment variable
3. `$NOTEBOOKLM_HOME/storage_state.json` file
4. `~/.notebooklm/storage_state.json` file (default)

**Note:** Cannot run `notebooklm login` when `NOTEBOOKLM_AUTH_JSON` is set.

## CLI Options

### Global Options

| Option | Description | Default |
|--------|-------------|---------|
| `--storage PATH` | Path to storage_state.json | `$NOTEBOOKLM_HOME/storage_state.json` |
| `--version` | Show version | - |
| `--help` | Show help | - |

### Viewing Configuration

See where your configuration files are located:

```bash
notebooklm status --paths
```

Output:
```
                Configuration Paths
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ File            ┃ Path                         ┃ Source    ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ Home Directory  │ /home/user/.notebooklm      │ default   │
│ Storage State   │ .../storage_state.json      │           │
│ Context         │ .../context.json            │           │
│ Browser Profile │ .../browser_profile         │           │
└─────────────────┴──────────────────────────────┴───────────┘
```

## Python API Configuration

### Default Initialization

```python
from notebooklm import NotebookLMClient

# Uses $NOTEBOOKLM_HOME/storage_state.json or ~/.notebooklm/storage_state.json
async with await NotebookLMClient.from_storage() as client:
    ...
```

### Custom Storage Path

```python
from pathlib import Path
from notebooklm import NotebookLMClient

# Specify a different storage file
async with await NotebookLMClient.from_storage(
    path=Path("./custom-storage.json")
) as client:
    ...
```

### Manual Authentication

```python
from notebooklm import NotebookLMClient, AuthTokens

# Provide tokens directly
auth = AuthTokens(
    cookies={"SID": "...", "HSID": "...", ...},
    csrf_token="SNlM0e_value",
    session_id="FdrFJe_value"
)

async with NotebookLMClient(auth) as client:
    ...
```

## Session Management

### Session Lifetime

Authentication sessions are tied to Google's cookie expiration:
- Sessions typically last several days to weeks
- Google may invalidate sessions for security reasons
- Rate limiting or suspicious activity can trigger earlier expiration

### Refreshing Sessions

If your session expires:

```bash
# Re-authenticate
notebooklm login
```

### Multiple Accounts

Use `NOTEBOOKLM_HOME` to maintain separate configurations:

```bash
# Work account
export NOTEBOOKLM_HOME=~/.notebooklm-work
notebooklm login
notebooklm list

# Personal account
export NOTEBOOKLM_HOME=~/.notebooklm-personal
notebooklm login
notebooklm list
```

Or use `--storage` for one-off overrides:

```bash
notebooklm --storage ~/.notebooklm/account1.json login
notebooklm --storage ~/.notebooklm/account1.json list
```

## CI/CD Configuration

### GitHub Actions (Recommended)

Use `NOTEBOOKLM_AUTH_JSON` for secure, file-free authentication:

```yaml
jobs:
  notebook-task:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install notebooklm-py
        run: pip install notebooklm-py

      - name: List notebooks
        env:
          NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_JSON }}
        run: notebooklm list
```

**Benefits:**
- No file writes needed
- Secret stays in memory only
- Clean, simple workflow

### Obtaining the Secret Value

1. Run `notebooklm login` locally
2. Copy the contents of `~/.notebooklm/storage_state.json`
3. Add as a GitHub repository secret named `NOTEBOOKLM_AUTH_JSON`

### Alternative: File-Based Auth

If you prefer file-based authentication:

```yaml
- name: Setup NotebookLM auth
  run: |
    mkdir -p ~/.notebooklm
    echo "${{ secrets.NOTEBOOKLM_AUTH_JSON }}" > ~/.notebooklm/storage_state.json
    chmod 600 ~/.notebooklm/storage_state.json

- name: List notebooks
  run: notebooklm list
```

### Session Expiration

Authentication sessions expire. For long-running CI pipelines:
- Refresh the secret periodically (every 1-2 weeks)
- Monitor for auth failures and alert

## Debugging

### Enable Verbose Output

Some commands support verbose output via Rich console:

```bash
# Most errors are printed to stderr with details
notebooklm list 2>&1 | cat
```

### Enable RPC Debug Logging

```bash
NOTEBOOKLM_DEBUG_RPC=1 notebooklm list
```

### Check Authentication

Verify your session is working:

```bash
# Should list notebooks or show empty list
notebooklm list

# If you see "Unauthorized" or redirect errors, re-login
notebooklm login
```

### Check Configuration Paths

```bash
# See where files are being read from
notebooklm status --paths
```

### Network Issues

The CLI uses `httpx` for HTTP requests. Common issues:

- **Timeout**: Google's API can be slow; large operations may time out
- **SSL errors**: Ensure your system certificates are up to date
- **Proxy**: Set standard environment variables (`HTTP_PROXY`, `HTTPS_PROXY`) if needed

## Platform Notes

### macOS

Works out of the box. Chromium is downloaded automatically by Playwright.

### Linux

```bash
# Install Playwright dependencies
playwright install-deps chromium

# Then install Chromium
playwright install chromium
```

### Windows

Works with PowerShell or CMD. Use backslashes for paths:

```powershell
notebooklm --storage C:\Users\Name\.notebooklm\storage_state.json list
```

Or set environment variable:

```powershell
$env:NOTEBOOKLM_HOME = "C:\Users\Name\custom-notebooklm"
notebooklm list
```

### WSL

Browser login opens in the Windows host browser. The storage file is saved in the WSL filesystem.

### Headless Servers & Containers

**Playwright is only required for the `notebooklm login` command.** All other operations use standard HTTP requests via `httpx`.

This means you can run notebooklm on headless servers, Docker containers, and CI/CD environments without Playwright—just copy a valid `storage_state.json` or use `NOTEBOOKLM_AUTH_JSON`.

```bash
# On headless machine - no Playwright needed
pip install notebooklm-py

# Copy auth from local machine, or use env var
scp ~/.notebooklm/storage_state.json user@server:~/.notebooklm/
# OR
export NOTEBOOKLM_AUTH_JSON='{"cookies": [...]}'

# All commands work except 'login'
notebooklm list
notebooklm ask "Summarize the sources"
```
