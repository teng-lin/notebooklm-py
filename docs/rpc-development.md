# RPC Development Guide

**Status:** Active
**Last Updated:** 2026-05-13

This guide covers everything about NotebookLM's RPC protocol: capturing calls, debugging issues, and implementing new methods.

---

## Protocol Overview

NotebookLM uses Google's `batchexecute` RPC protocol.

### Key Concepts

| Term | Description |
|------|-------------|
| **batchexecute** | Google's internal RPC endpoint |
| **RPC ID** | 6-character identifier (e.g., `wXbhsf`, `s0tc2d`) |
| **f.req** | URL-encoded JSON payload |
| **at** | CSRF token (SNlM0e value) |
| **Anti-XSSI** | `)]}'` prefix on responses |

### Protocol Flow

```
1. Build request: [[[rpc_id, json_params, null, "generic"]]]
2. Encode to f.req parameter
3. POST to /_/LabsTailwindUi/data/batchexecute
4. Strip )]}' prefix from response
5. Parse chunked JSON, extract result
```

### Source of Truth

- **RPC method IDs:** `src/notebooklm/rpc/types.py`
- **Payload structures:** `docs/rpc-reference.md`

---

## Capturing RPC Calls

### Manual Capture (Chrome DevTools)

Best for quick investigation and bug reports.

1. Open Chrome → Navigate to `https://notebooklm.google.com/`
2. Open DevTools (`F12` or `Cmd+Option+I`)
3. Go to **Network** tab
4. Configure:
   - [x] **Preserve log**
   - [x] **Disable cache**
5. Filter by: `batchexecute`
6. **Perform ONE action** (isolate the exact RPC call)
7. Click the request to inspect

**From the request:**
- **Headers tab → URL `rpcids`**: The RPC method ID
- **Payload tab → `f.req`**: URL-encoded payload
- **Response tab**: Starts with `)]}'` prefix

### Decoding the Payload

**Browser console:**
```javascript
const encoded = "...";  // Paste f.req value
const decoded = decodeURIComponent(encoded);
const outer = JSON.parse(decoded);
console.log("RPC ID:", outer[0][0][0]);
console.log("Params:", JSON.parse(outer[0][0][1]));
```

**Python:**
```python
import json
from urllib.parse import unquote

def decode_f_req(encoded: str) -> dict:
    decoded = unquote(encoded)
    outer = json.loads(decoded)
    inner = outer[0][0]
    return {
        "rpc_id": inner[0],
        "params": json.loads(inner[1]) if inner[1] else None,
    }
```

### Playwright Automation

Best for systematic capture and CI integration.

```python
from playwright.async_api import async_playwright
import json
from urllib.parse import unquote, parse_qs

async def setup_capture_session():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch_persistent_context(
        user_data_dir="./browser_state",
        headless=False,
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()
    captured_rpcs = []

    async def handle_request(request):
        if "batchexecute" in request.url:
            post_data = request.post_data
            if post_data and "f.req" in post_data:
                params = parse_qs(post_data)
                f_req = params.get("f.req", [None])[0]
                if f_req:
                    decoded = decode_f_req(f_req)
                    captured_rpcs.append(decoded)

    page.on("request", handle_request)
    return page, captured_rpcs
```

---

## Debugging Issues

### Enable Debug Mode

```bash
# See what RPC IDs the server returns
NOTEBOOKLM_DEBUG_RPC=1 notebooklm <command>
```

Output:
```
DEBUG: Looking for RPC ID: Ljjv0c
DEBUG: Found RPC IDs in response: ['Ljjv0c']
```

If IDs don't match, the method ID has changed - report it in a GitHub issue.

### Common Scenarios

#### "Session Expired" Errors

```python
# Check CSRF token
print(client.auth.csrf_token)

# Refresh auth
await client.refresh_auth()
```

**Solution:** Re-run `notebooklm login`

#### RPC Method Returns None

**Causes:**
- Rate limiting (Google returns empty result)
- Wrong RPC method ID
- Incorrect parameter structure

**Debug:**
```python
from notebooklm.rpc import decode_response

raw_response = await http_client.post(...)
print("Raw:", raw_response.text[:500])

result = decode_response(raw_response.text, "METHOD_ID")
print("Parsed:", result)
```

#### Parameter Order Issues

RPC parameters are **position-sensitive**:

```python
# WRONG - missing positional elements
params = [value, notebook_id]

# RIGHT - all positions filled
params = [value, notebook_id, None, None, settings]
```

**Debug:** Compare your params with captured traffic byte-by-byte.

#### Nested List Depth

Source IDs have different nesting requirements:

```python
# Single nesting (some methods)
["source_id"]

# Double nesting
[["source_id"]]

# Triple nesting (artifact generation)
[[["source_id"]]]

# Quad nesting (get_source_guide)
[[[["source_id"]]]]
```

**Debug:** Capture working traffic and count brackets.

### Response Parsing

```python
import json
import re

def parse_response(text: str, rpc_id: str):
    """Parse batchexecute response."""
    # Strip anti-XSSI prefix
    if text.startswith(")]}'"):
        text = re.sub(r"^\)\]\}'\r?\n", "", text)

    # Find wrb.fr chunk for our RPC ID
    for line in text.split("\n"):
        try:
            chunk = json.loads(line)
            if chunk[0] == "wrb.fr" and chunk[1] == rpc_id:
                result = chunk[2]
                return json.loads(result) if isinstance(result, str) else result
        except (json.JSONDecodeError, IndexError):
            continue
    return None
```

---

## Adding New RPC Methods

### Workflow

```
1. Capture → 2. Decode → 3. Implement → 4. Test → 5. Document
```

### Step 1: Capture

Use Chrome DevTools or Playwright (see above).

**What to capture:**
- RPC ID from URL `rpcids` parameter
- Decoded `f.req` payload
- Response structure

### Step 2: Decode

Document each position in the params array:

```python
# Example: ADD_SOURCE for URL
params = [
    [[None, None, [url], None, None, None, None, None]],  # 0: Source data
    notebook_id,   # 1: Notebook ID
    [2],           # 2: Fixed flag
    None,          # 3: Optional settings
]
```

Key patterns:
- **Nested source IDs:** Count brackets carefully
- **Fixed flags:** Arrays like `[2]`, `[1]` that don't change
- **Optional positions:** Often `None`

### Step 3: Implement

**Add RPC method ID** (`src/notebooklm/rpc/types.py`):
```python
class RPCMethod(str, Enum):
    NEW_METHOD = "AbCdEf"  # 6-char ID from capture
```

**Add client method** (appropriate `_*.py` file):
```python
async def new_method(self, notebook_id: str, param: str) -> SomeResult:
    """Short description.

    Args:
        notebook_id: The notebook ID.
        param: Description.

    Returns:
        Description of return value.
    """
    params = [
        param,           # Position 0
        notebook_id,     # Position 1
        [2],             # Position 2: Fixed flag
    ]

    result = await self._core.rpc_call(
        RPCMethod.NEW_METHOD,
        params,
        source_path=f"/notebook/{notebook_id}",
    )

    if result is None:
        return None
    return SomeResult.from_api_response(result)
```

**Add dataclass if needed** (`src/notebooklm/types.py`):
```python
@dataclass
class SomeResult:
    id: str
    title: str

    @classmethod
    def from_api_response(cls, data: list[Any]) -> "SomeResult":
        return cls(id=data[0], title=data[1])
```

### Step 4: Test

**Unit test** (`tests/unit/`):
```python
def test_encode_new_method():
    params = ["value", "notebook_id", [2]]
    result = encode_rpc_request(RPCMethod.NEW_METHOD, params)
    assert "AbCdEf" in result
```

**Integration test** (`tests/integration/`):
```python
@pytest.mark.asyncio
async def test_new_method(mock_client):
    mock_response = ["result_id", "Result Title"]
    with patch('notebooklm._core.ClientCore.rpc_call', new_callable=AsyncMock) as mock:
        mock.return_value = mock_response
        result = await mock_client.some_api.new_method("nb_id", "param")
        assert result.id == "result_id"
```

**E2E test** (`tests/e2e/`):
```python
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_new_method_e2e(client, read_only_notebook_id):
    result = await client.some_api.new_method(read_only_notebook_id, "param")
    assert result is not None
```

### Step 5: Document

Update `docs/rpc-reference.md`:

```markdown
### NEW_METHOD (`AbCdEf`)

**Purpose:** Short description

**Params:**
```python
params = [
    some_value,      # 0: Description
    notebook_id,     # 1: Notebook ID
    [2],             # 2: Fixed flag
]
```

**Response:** Description of response structure

**Source:** `_some_api.py:123`
```

---

## Common Pitfalls

### Wrong nesting level

Different methods need different source ID nesting. Check similar methods.

### Position sensitivity

Params are arrays, not dicts. Position matters:

```python
# WRONG - missing position 2
params = [value, notebook_id, settings]

# RIGHT - explicit None for unused positions
params = [value, notebook_id, None, settings]
```

### Forgetting source_path

Some methods require `source_path` for routing:

```python
# May fail without source_path
await self._core.rpc_call(RPCMethod.X, params)

# Correct
await self._core.rpc_call(
    RPCMethod.X,
    params,
    source_path=f"/notebook/{notebook_id}",
)
```

### Response parsing

API returns nested arrays. Print raw response first:

```python
result = await self._core.rpc_call(...)
print(f"DEBUG: {result}")  # See actual structure
```

---

## Checklist

- [ ] Captured RPC ID and params structure
- [ ] Added to `RPCMethod` enum in `rpc/types.py`
- [ ] Implemented method in appropriate `_*.py` file
- [ ] Added dataclass if needed in `types.py`
- [ ] Added CLI command if needed
- [ ] Unit test for encoding
- [ ] Integration test with mock
- [ ] E2E test (manual verification OK for rare operations)
- [ ] Updated `rpc-reference.md`

---

## LLM Agent Workflow

For AI agents discovering new RPC methods:

### Context

```
NotebookLM Protocol Facts:
- Endpoint: /_/LabsTailwindUi/data/batchexecute
- RPC IDs are 6-character strings (e.g., "wXbhsf")
- Payload: [[[rpc_id, json_params, null, "generic"]]]
- Response has )]}' anti-XSSI prefix
- Parameters are position-sensitive arrays

Source of Truth:
- Canonical RPC IDs: src/notebooklm/rpc/types.py
- Payload structures: docs/rpc-reference.md
```

### Discovery Prompt Template

```
Task: Discover the RPC call for [ACTION_NAME]

Steps:
1. Identify the UI element that triggers this action
2. Set up network interception for batchexecute
3. Trigger the UI action
4. Capture the RPC request

Document:
- RPC ID (6-character string)
- Payload structure with parameter positions
- Source ID nesting pattern
- Response structure
```

### Validation

```python
async def validate_rpc_call(rpc_id: str, params: list, expected_action: str):
    from notebooklm import NotebookLMClient

    async with await NotebookLMClient.from_storage() as client:
        result = await client._rpc_call(RPCMethod(rpc_id), params)

    assert result is not None, f"RPC {rpc_id} returned None"
    return {"rpc_id": rpc_id, "action": expected_action, "status": "verified"}
```

## RPC Health Check Triage Policy

The `rpc-health.yml` workflow runs daily (07:00 UTC) and opens an issue on any
detected RPC ID mismatch or auth failure:

- **RPC ID mismatch** issues: labeled `bug, rpc-breakage, automated`.
- **Auth failure** issues: labeled `bug, automated` (no `rpc-breakage` label —
  auth is an operational concern, not a protocol break).

Routing:

- **Maintainer assignment**: Issues land in the `teng-lin/notebooklm-py`
  default issue inbox. The maintainer triages within 24 hours during business
  days. (No auto-assignee — the project has a single maintainer and
  auto-assignment adds noise.)
- **Acknowledged-but-deferred**: If an upstream RPC change is observed but
  the library still functions for the majority of users (e.g., one optional
  field renamed), the maintainer closes the issue with the `acknowledged`
  label and links the PR that resolves it.
- **Notifying users**: If the breakage affects an RPC most users invoke
  (e.g., `LIST_NOTEBOOKS`, `CREATE_NOTEBOOK`), the maintainer additionally
  files a release-note draft + pins the issue.

If you see an `rpc-breakage` issue sitting unattended for >7 days, ping the
maintainer in a comment — it likely fell out of the inbox. The intent of this
workflow is fast detection, not perpetual auto-noise.
