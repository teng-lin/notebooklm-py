# RPC Development Guide

**Status:** Active
**Last Updated:** 2026-09-02

This guide covers the Web backend's batchexecute protocol: capturing calls,
debugging issues, and implementing Web methods. For Android gRPC/protobuf work,
start with the [Android evidence index](android/README.md), then use
[`android/capture.md`](android/capture.md) and the checked-in proto/evidence ledger.
Typed public namespaces may implement both backends, but the wire-development
workflows are intentionally separate.

See also: [Python API Reference](python-api.md)

---

## Protocol Overview

NotebookLM's Web frontend uses Google's `batchexecute` RPC protocol. The
explicit Android backend uses bearer-authenticated gRPC instead; `RPCMethod`
and `client.rpc_call(...)` remain Web-specific even when the typed namespace
graph is Android-selected.

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
- **Payload builders:** the owning implementation modules, for example
  `_web/params/notebooks.py::build_create_notebook_params`,
  `_web/params/artifacts.py`, `_web/params/sources.py`, `_web/sources/add.py`,
  `_web/params/labels.py`, and `_web/params/collections.py`
- **Golden payload tests:** `tests/unit/test_rpc_golden_payloads.py` and
  feature-specific unit tests such as `tests/unit/test_label_params.py`
- **Human reference:** `docs/rpc-reference.md`, updated after the builder and
  tests land
- **Android contracts:** `docs/android/endpoints.md` and
  `docs/android/proto-evidence-ledger.md`, with adapter paths pinned by Android
  manifest/descriptor tests

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

    def handle_request(request):
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
# decode_response is an internal RPC helper (notebooklm.rpc.* is internal per
# docs/stability.md); import it from its defining module for contributor debugging.
from notebooklm._web.wire.decoder import decode_response

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

The workflow below is for a Web batchexecute method. If the public feature must
also work under explicit Android selection, add or extend the corresponding
`Android*API`, protobuf overlay/evidence entry, and Android unit/cassette tests;
do not inject a Web operation collaborator into the Android namespace graph.

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
# Example: ADD_SOURCE for URL after the Gemini-3.5 wire-shape migration
params = [
    [[None, None, [url], None, None, None, None, None, None, None, 1]],
    notebook_id,
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
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
        param,  # Position 0
        notebook_id,  # Position 1
        [2],  # Position 2: Fixed flag
    ]

    result = await self._rpc.rpc_call(
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
    assert result[0][0][0] == "AbCdEf"
```

**Unit test with a fake RPC executor** (`tests/unit/`):
```python
@pytest.mark.asyncio
async def test_new_method():
    mock_response = ["result_id", "Result Title"]
    fake = make_fake_core(rpc_call=AsyncMock(return_value=mock_response))
    api = SomeAPI(fake.rpc_executor)

    result = await api.new_method("nb_id", "param")

    assert result.id == "result_id"
    fake.rpc_executor.rpc_call.assert_awaited_once()
```

**VCR-backed integration test** (`tests/integration/`) or authenticated E2E
test (`tests/e2e/`):
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
### RPC: NEW_METHOD (`AbCdEf`)

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

**Source:** `_some_api.py::new_method()`
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
await self._rpc.rpc_call(RPCMethod.X, params)

# Correct
await self._rpc.rpc_call(
    RPCMethod.X,
    params,
    source_path=f"/notebook/{notebook_id}",
)
```

### Response parsing

API returns nested arrays. Print raw response first:

```python
result = await self._rpc.rpc_call(...)
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
- [ ] If the typed public method is backend-neutral, implemented and tested the
  Android adapter or explicitly documented why the method is Web-only

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
async def validate_root_rpc_call(method_name: str, params: list):
    from notebooklm import NotebookLMClient
    from notebooklm.rpc import RPCMethod

    async with NotebookLMClient.from_storage() as client:
        # Public raw calls use the default root source path. For notebook-scoped
        # calls that need source_path="/notebook/<id>", prefer the typed
        # namespace API or a focused internal test around RpcExecutor.
        result = await client.rpc_call(RPCMethod[method_name], params)

    assert result is not None, f"RPC {method_name} returned None"
    return {"method": method_name, "status": "verified"}
```

## RPC Health Check Triage Policy

The `rpc-health.yml` workflow runs daily for `main` (07:00 UTC). Release branch
health checks are manual via `custom_branch=release/vX.Y.Z`. The workflow opens
an issue on any detected RPC ID mismatch, auth failure, or non-transient RPC
error:

- **RPC ID mismatch** issues (exit code 1): labeled `bug, rpc-breakage, automated`.
- **Auth failure** issues (exit code 2): labeled `bug, automated` (no `rpc-breakage`
  label — auth is an operational concern, not a protocol break).
- **Frontend bundle drift** is a separate live monitor. Its exit code 1 is
  reserved for confirmed ABSENT RPC IDs or CHANGED/STALE studio enums. If its
  authenticated homepage request instead lands on login, CookieMismatch, or the
  region/anti-abuse gate—or the app/CDN cannot be read—it exits 2, says that no
  drift conclusion was possible, and joins the authentication/infrastructure
  issue lane. The script also writes a classified outcome file; the workflow
  opens `Studio enum / RPC drift detected` only for the explicit `drift`
  outcome. A Python, dependency, or runner failure that exits 1 before writing
  that outcome is therefore treated as infrastructure, never as protocol drift.
- **Non-transient ERROR detected** issues (exit code 3): labeled `rpc-error, bug,
  automated`. Opened when `check_rpc_health.py` surfaces failures that survive
  the rate-limit / `RESOURCE_EXHAUSTED` filter (timeouts, parse failures,
  unexpected HTTP errors). The issue body lists the affected method IDs
  extracted from the report, so triage can start without re-running the check.
  See the `Extract failing methods for ERROR issue` step in
  `.github/workflows/rpc-health.yml` for the body-assembly logic.
- **Stale build label** issues (exit code 5): labeled `rpc-breakage, automated`.
  See the build-label lane below.

### Build-label lane (`bl` / `_env.DEFAULT_BL`)

`bl` is the frontend build label sent on the chat streaming endpoint. It is a
pinned constant, and pinned constants nobody re-verifies are this project's #1
breakage class — but unlike a wrong RPC ID, which fails loudly and immediately, a
stale `bl` is accepted silently. Cassettes replay whatever was recorded, so the
entire offline suite passes no matter how old the pin gets. It reached five
months (154 label-days) of drift before anyone looked
([#2073](https://github.com/teng-lin/notebooklm-py/issues/2073)).

Each nightly run fetches the app shell, extracts the label Google actually
serves, and scores the pin against it:

| Verdict | Meaning | Exit |
| --- | --- | --- |
| `CURRENT` | the pin is exactly what is served | 0 |
| `DRIFTED` | pin differs but is within `_env.BUILD_LABEL_STALE_AFTER_DAYS` (90) | 0 |
| `STALE` | pin trails the served label by more than that window | 5 |
| `UNKNOWN` | no label could be read (signed out, transport failure, unrecognized shell) | 0 |

- **`DRIFTED` is the steady state.** Google ships a new build roughly weekly and
  the pin is not expected to chase every one; a tighter window would alarm
  continuously and teach everyone to ignore the lane.
- **The verdict compares label dates, never the wall clock**, so it depends only
  on what was served — a delayed or replayed run cannot age into an alarm.
- **Exit 5 sits below every live-breakage code** (mismatch, auth, non-transient
  error, customization-table drift). A stale pin is maintenance, and it must never mask an
  outage.
- **Redirects are followed by hand**, at most two hops, and only to an `https`
  personal app host at the site root — the lane never carries the session jar
  somewhere it did not intend to go, and never onto cleartext. The default host
  serves the shell directly; the legacy host 302s to it, so only a run pointed at
  the rollback host takes a hop at all, and anything past the second reports
  `more than 2 redirects`. The sign-in bounce (`/login?continue=…`) ends the walk
  with `UNKNOWN`: "this run was not signed in" is not evidence about the build
  label.
- An active `NOTEBOOKLM_BL` override does not change the verdict — the lane always
  scores the committed `DEFAULT_BL`, since that is what ships to users — but the
  report says the override was in effect.

**To clear a `STALE` verdict:** take the served label from the report (or run the
probe below) and bump `DEFAULT_BL` in `src/notebooklm/_env.py`.

```python
import asyncio, httpx
from notebooklm._auth.cookies import _build_httpx_cookies_from_storage_strict
from notebooklm._env import DEFAULT_BL, extract_build_label, get_base_url


async def main():
    # The strict loader is deliberate: build_httpx_cookies_from_storage triggers a
    # PSIDTS RotateCookies round-trip and a disk write, so it is not safe here.
    jar = _build_httpx_cookies_from_storage_strict(None)
    async with httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=60.0) as c:
        r = await c.get(f"{get_base_url()}/")
    print("pinned:", DEFAULT_BL)
    print("served:", extract_build_label(r.text))


asyncio.run(main())
```

**Known and deliberately not acted on:** the server does not validate this value.
Measured live on 2026-08-04, the streaming endpoint returned a complete, cited
answer for the pinned label, the served label, and a fabricated
`…_19700101.00_p0` alike. So the lane is not guarding a live dependency today —
it exists so the pin cannot rot unwatched again, and so that the day chat does
break on it, the report already says how far behind it had drifted.

### Rebrand-host lane (`notebook.google.com`)

The same nightly run also probes the post-rebrand host — batchexecute and
`GenerateFreeFormStreamed` — in a **separate reporting lane**:

- It carries **no exit code**. Its probes never enter the `CheckResult` list, so
  `compute_exit_code` cannot see them. This is deliberate and load-bearing: the
  "Non-transient ERROR detected" issue is deduped **by title alone**, so a probe
  that legitimately fails every night would open one issue and then suppress
  every later main-lane degradation issue filed under the same title.
- It reports a **state change** (for example, `batchexecute:
  PRESENT->ABSENT`), not a recurring error, against the previous run's state.
  That state is cached between runs; a cache miss falls back to the checked-in
  last-acknowledged status for each capability. An unchanged capability files
  nothing.
- On a change it opens its own issue, **"Rebrand host RPC availability
  changed"** (label `automated`), with its own dedup search.
- `UNKNOWN` (transport failure, 429, 5xx) is never recorded: a flake carries the
  previous state forward instead of manufacturing a transition.
- It runs **last** in the check and is paced like the method loop, so its two
  extra requests cannot push the account into a rate limit that would then be
  attributed to a main-lane probe.

**Recorded decision (when the lane was introduced):** this was the first time
the project's CI credentials were presented to `notebook.google.com`. Both
hosts are Google's and are origins of the same app, so the exposure was the same
credential to the same operator — but it was a deliberate choice, written down
rather than arriving as a side effect.

Two flags support it:

```bash
# Point the WHOLE run at a specific personal app host. Manual investigation
# only — validated against notebooklm._env.PERSONAL_APP_HOSTS, and the nightly
# stays on the default so the main, exit-coded signal exercises that host.
uv run python scripts/check_rpc_health.py --base-url https://notebook.google.com

# Where the lane reads/writes its previous state (omit: baseline-only, no write).
uv run python scripts/check_rpc_health.py --rebrand-state-file rebrand-state.json
```

**Not answered by this lane:** whether the rebrand host serves `/upload/_/`
(Scotty). `check_rpc_health.py` exercises `ADD_SOURCE_FILE` as an RPC only and
issues no upload POST anywhere, so the report prints `upload NOT_PROBED` every
run. Answering it needs a manual authenticated capture (upload-session start
only, no bytes), scrubbed via `tests/cassette_patterns.py` before it leaves the
machine.

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
