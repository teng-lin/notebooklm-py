# MCP server guide

> **Experimental / preview.** The MCP server ships behind the optional `mcp` extra. Its
> tool surface (names, parameters, output shapes) is **not** covered by the library's semver
> guarantees and may change between releases. `pip install notebooklm-py` is unaffected — the
> server and its dependencies only arrive with the `mcp` extra.

The MCP server exposes NotebookLM to any [Model Context Protocol](https://modelcontextprotocol.io)
client (Claude Desktop, Claude Code, Cursor, Windsurf, …) as a set of **32 tools** — manage
notebooks and sources, chat over a notebook's sources, generate and download studio artifacts,
and run deep research. It is a thin adapter over the same business logic the CLI uses, so it
behaves identically to `notebooklm <command>`.

## Install

The server is behind the `mcp` extra (pulls in `fastmcp`):

```bash
pip install "notebooklm-py[mcp]"
# or run with no install, straight from PyPI:
uvx --from "notebooklm-py[mcp]" notebooklm-mcp --help
```

## Authenticate (once)

The server reuses the CLI's stored credentials — it does **not** log in on its own. Authenticate
once before starting it:

```bash
notebooklm login
# or, if you didn't install the package:
uvx --from "notebooklm-py[mcp]" notebooklm login
```

Credentials are stored per profile under `~/.notebooklm/`. The server binds the **active profile**
at startup (override with `--profile`, below). See [configuration.md](configuration.md) for profiles
and multi-account setup.

## Connect a client

The fastest path is the auto-config command, which writes the server block into a client's MCP
config (idempotent, never clobbers other servers):

```bash
notebooklm mcp install claude-desktop   # or: claude-code | cursor | windsurf
```

| Client | Config written |
|--------|----------------|
| `claude-desktop` | `claude_desktop_config.json` (per-OS location) |
| `claude-code` | `~/.claude.json` (user scope) |
| `cursor` | `~/.cursor/mcp.json` |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` |

It writes a block that launches the server via `uvx` (so only `uv` needs to be on the host):

```jsonc
{
  "mcpServers": {
    "notebooklm": {
      "command": "uvx",
      "args": ["--from", "notebooklm-py[mcp]", "notebooklm-mcp"]
    }
  }
}
```

Restart the client after installing. For a one-click Claude Desktop bundle,
download `notebooklm-mcp.mcpb` from the
[latest release](https://github.com/teng-lin/notebooklm-py/releases/latest)
(**Assets**) and use "Install Extension"; see
[`desktop-extension/README.md`](../desktop-extension/README.md) for details.

## Run it directly

The console script is `notebooklm-mcp`:

```bash
notebooklm-mcp                         # stdio transport (default — for desktop hosts)
notebooklm-mcp --profile work          # bind a specific auth profile
notebooklm-mcp --transport http        # loopback streamable-HTTP on 127.0.0.1:9420
notebooklm-mcp --transport http --port 9000
```

| Flag | Default | Notes |
|------|---------|-------|
| `--profile` | active profile | which stored auth profile the process binds |
| `--transport` | `stdio` | `stdio` (subprocess hosts) or `http` (loopback) |
| `--host` | `127.0.0.1` | http only; non-loopback is **refused** unless `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1` |
| `--port` | `9420` | http only |
| `--log-level` | `INFO` | logs go to **stderr**; stdout stays pure JSON-RPC |

There is no `--token` flag — the HTTP bearer token is **env-only**
(`NOTEBOOKLM_MCP_TOKEN`) so it cannot leak via `ps aux`.

`stdio` is right for Claude Desktop/Code, Cursor, and Windsurf (they launch the server as a
subprocess). Use `http` for a local web client or to share one running server across clients on
the same machine. The HTTP transport is loopback-only by default; binding to a non-loopback
address requires **both** the explicit `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND=1` override **and** a
`NOTEBOOKLM_MCP_TOKEN` — the server fails closed (refuses to start) on a network bind without a
token, since it fronts a full Google account.

## Remote deployment (Docker + a tunnel)

Because master-token auth keeps the session alive unattended (no browser), the HTTP transport can
run as a **remote connector** reachable from Claude Code, Claude Desktop, claude.ai, mobile, and ChatGPT.
The [`deploy/`](../deploy/) directory ships a turn-key Docker + Compose stack with a **tunnel
sidecar** — pick one via a Compose profile — so you get HTTPS with **no public IP, no open ports,
and no TLS certificate to manage** (the tunnel terminates TLS at its edge).

**→ The full step-by-step lives in [`deploy/README.md`](../deploy/README.md)** — run it from
inside `deploy/` (`make setup` → finish the one manual tunnel step → `make up`), where the Compose
stack, `Makefile`, and `.env.example` it references sit. It walks both tunnels end to end:
**Cloudflare** (needs a domain in your Cloudflare account) and **Tailscale Funnel** (no domain — a
free, stable `*.ts.net` HTTPS hostname). The rest of this section is the two things worth knowing
before you start: the auth model and remote file transfer.

**Two auth methods coexist on one `/mcp`** (FastMCP `MultiAuth`):
- **Claude Code / Desktop** → the static `NOTEBOOKLM_MCP_TOKEN` bearer (an `Authorization` header).
- **claude.ai (web/mobile) and ChatGPT** (Developer Mode) → optional **self-hosted OAuth** (one
  password, no external IdP): set `NOTEBOOKLM_MCP_OAUTH_PASSWORD` (≥16 random chars) +
  `NOTEBOOKLM_MCP_OAUTH_BASE_URL` (the **bare public origin**, no `/mcp`). Both connector UIs are
  OAuth-only (no bearer field). Unset → bearer-only (Claude Code/Desktop still work).

Use a **dedicated/throwaway Google account** — the mounted `master_token.json` is a durable
full-account credential. Multi-tenant hosting is out of scope for this single-tenant setup.

### File upload & download (remote)

The MCP/JSON-RPC channel can't carry large binaries, so over a remote connector
`source_add type=file` and `studio_download` broker a **short-lived signed URL**
served by the same container; your browser does the byte transfer (see
[ADR-0024](adr/0024-mcp-remote-file-transfer.md)). This is the standard pattern for
remote MCP file transfer — MCP has no native file-upload primitive, and its native
download (binary Resources) is capped far below a podcast/video. (A **small** file
can skip the signed URL entirely — see `source_upload_bytes` below.)

**Enable it:** set `NOTEBOOKLM_MCP_PUBLIC_URL` to your bare public origin (the same
host as the tunnel, no `/mcp`). It falls back to `NOTEBOOKLM_MCP_OAUTH_BASE_URL`, so
if you configured claude.ai OAuth above, **file transfer is already on**. Unset on a
bearer-only deploy → the two file tools return a clear "not configured" error
(everything else still works; the server does not refuse to start).

- **Upload a local file:** `source_add type=file` returns an `upload_required` link.
  Open it in your browser, pick the file, and it's added to the notebook. (Claude can
  also `PUT` a file it already holds to that link from its **code-execution sandbox** —
  but that requires Code Execution enabled **and your server domain whitelisted** in
  claude.ai Settings → Capabilities → additional allowed domains, or the `PUT` fails.)
- **Hand a small file's bytes in-channel (no signed URL):** when an agent holds the
  bytes but can complete *neither* upload path — e.g. its egress is blocked, so the
  `agent_upload` POST fails, and no human device has the file — `source_upload_bytes`
  takes the file as base64 (≤ 10,000 chars, ≈ 7 KB) and the connector adds it
  server-side, returning the source directly. It works on any transport and needs no
  `NOTEBOOKLM_MCP_PUBLIC_URL`; a larger file must use the `source_add type=file`
  signed-URL flow above.
- **Download an artifact:** `studio_download` returns a `download_ready` link (a
  clickable `resource_link`); open it to stream the podcast/video/PDF to your device.
- Links are HMAC-signed and short-lived (upload 15 min, download 30 min) and expire on
  a server restart. Google Drive (`source_add` with a Drive id) remains a no-browser
  alternative for adding files. stdio (local) installs are unchanged — they still read
  and write real local paths directly.

## Core concepts

These conventions hold across every tool:

- **JSON by default.** Read/wait tools, including `source_read`, `source_wait`, and
  `source_add_and_wait`, return a JSON text content block plus the same
  `structured_content`. A `resource_link` appears only when a tool explicitly brokers
  file transfer, such as `studio_download`.
- **Name *or* ID.** Every `notebook`/`source`/`note`/`artifact` argument accepts a human title **or**
  an ID. Both resolve by prefix: an exact title wins, otherwise a **unique title prefix** matches
  (so `"Scientific"` finds `"Scientific PDF Parsing — …"`), and likewise a full ID or a unique ID
  prefix. Use the matching `*_list` tool to discover them. An ambiguous name or prefix returns a
  `VALIDATION` error listing the candidates so you can retry with an exact title or ID. When a name
  lookup *fails* but is close to a real title — a punctuation-only slip such as a hyphen typed for an
  em-dash (`—`) or a normal space for a non-breaking one — the error's `Did you mean: …` hint names
  up to three near-miss candidates, each with its **title and id** inline, so you can retry with the
  full title or id instead of guessing (a label near-miss reached via `source_list(label=…)` gets the
  same enrichment on its `VALIDATION` error).
- **Canonical IDs come back.** Every response echoes the canonical `notebook_id` (and, where a
  tool resolves them, the `source_ids` scope / `artifact_id`) — so a call made by *name* hands you
  the id to chain the next call on.
- **Strict IDs-only mode (opt-in).** Set `NOTEBOOKLM_MCP_STRICT_IDS=1` on the server to require a
  **full canonical id** for every `notebook`/`source`/`note`/`artifact` reference: names, titles, and
  short id prefixes are rejected with a `VALIDATION` error *before* any list call. This trades the
  convenience above for deterministic, fail-fast behavior in long-lived automation, where a prefix or
  title that is unique today can quietly resolve to a different (or ambiguous) entity tomorrow. Off by
  default. (Governs every notebook/source/note/artifact reference — including studio `item` and
  `studio_download`'s `artifact_id`; the `source_list(label=…)` name filter is out of scope.)
- **Destructive tools need confirmation.** `notebook_delete`, `source_delete`,
  `studio_delete`, and `share_remove_user` take `confirm` (default `false`). Called without it, they return a `needs_confirmation` preview
  (with the resolved title) and delete **nothing**; call again with `confirm=true` to execute.
- **Sharing-widening tools need confirmation too.** `share_set_user` (every grant/regrade) and
  `share_set_access` when it would widen link access (`public=true` on a currently-restricted
  notebook) take `confirm` (default `false`) and return a `needs_confirmation` preview instead of
  mutating; call again with `confirm=true` to apply. Restricting (`public=false`) and
  `view_level`-only changes are not gated. These tools are *not* flagged `destructiveHint` — the
  gate is on the widening direction only.
- **Long-running work is non-blocking.** `studio_generate` returns immediately with a `task_id`;
  poll `studio_status` until it's complete, then `studio_download`. Research is the same shape:
  `research_start` → `research_status` → `research_import`.
- **Mutation envelope.** Synchronous create/rename/update/delete tools return a top-level
  `status` string naming the outcome — one of `created`, `renamed`, `updated`, `deleted`,
  `removed`, `added`, `imported`, `cancel_requested`, `configured` (plus the `needs_confirmation` /
  `upload_required` / `download_ready` flow values) — alongside the affected id(s). An agent can
  branch on `status` uniformly instead of learning a different success shape per tool. Two
  carve-outs: the **long-running starters** `studio_generate` / `research_start` return a
  `task_id` (the handle *is* the result — poll it) rather than a mutation status — as does the
  re-runner `studio_retry` (its `task_id` equals the artifact id); and the **read** tools
  `studio_status` / `research_status` key `status` to a lifecycle vocabulary
  (`in_progress` / `completed` / …), a *different* enum. (Batch
  `source_add` reports `added` once ≥1 succeeded, `error` if all failed — see the `added` /
  `failed` tallies + per-item `results[].status` for partial outcomes.)
- **Structured errors.** Failures arrive as `CODE: message (retriable=…)`, where `CODE` is one of
  `AUTH`, `RATE_LIMITED`, `NOT_FOUND`, `VALIDATION`, `TIMEOUT`, `NETWORK`, `SERVER`, `RPC`,
  `CONFIG`, `NOTEBOOK_LIMIT`, `ARTIFACT_TIMEOUT`, `SOURCE_MUTATION`, `ERROR`, or `UNEXPECTED`. The
  `retriable` flag tells an agent whether a retry could succeed (e.g. `RATE_LIMITED`, `TIMEOUT`,
  `NETWORK`). Many errors also carry an actionable `hint` (e.g. `AUTH → run notebooklm login`); a
  near-miss name lookup puts its `Did you mean: …` candidates (title + id) in that hint (see
  **Name *or* ID** above).

## Workflows

The examples below are MCP **tool calls** an agent makes (not shell commands).

### Add sources and ask a question

```text
nb = notebook_create(title="Quantum Computing")
source_add(notebook="Quantum Computing", source_type="url", url="https://arxiv.org/abs/...")
source_add(notebook="Quantum Computing", source_type="text", title="Notes", text="...")
source_wait(notebook="Quantum Computing")                 # block until sources finish processing
chat_ask(notebook="Quantum Computing", question="What are the open problems?")
```

`source_type` is one of `url`, `text`, `file` (local `path`), `drive` (a
`document_id` + `mime_type`), or `youtube`. URL and YouTube adds reject
internal/loopback hosts by default; pass `allow_internal=true` only for
deliberate local NotebookLM tests. `chat_ask` continues the most-recent
conversation unless you pass a `conversation_id`.

To add ONE source and block until it finishes processing in a single call, use
`source_add_and_wait` — it composes single-mode `source_add` + `source_wait`, so
you skip the add→wait round-trip. It takes the same single-mode add inputs plus
the `timeout`/`interval` wait knobs, and returns the `source_wait` aggregate plus
a top-level `source_id` (always present — the source persists even if the wait
times out or the import fails, so you can retry or delete it):

```text
source_add_and_wait(notebook="Quantum Computing", source_type="url",
                    url="https://arxiv.org/abs/...")
# → {"notebook_id": ..., "ok": true, "ready": [{...}], "timed_out": [], "failed": [],
#    "not_found": [], "source_id": ...}
```

It is single-source only (no `urls` batch) and cannot one-shot a **remote** `file`
upload (that upload is a separate step — use `source_add(source_type="file")` then
`source_wait`, or `source_upload_bytes` for a tiny file).

To ingest many URLs at once, pass `urls` (batch mode) instead of `source_type`
— one call instead of one round-trip each. The response is an explicit per-item
list so a partial failure is never hidden behind a single success flag:

```text
source_add(notebook="Quantum Computing", urls=[
    "https://arxiv.org/abs/2401.00001",
    "https://www.youtube.com/watch?v=...",
])
# → {"notebook_id": ..., "added": 2, "failed": 0,
#    "results": [{"input": "https://arxiv.org/abs/2401.00001", "status": "added", "source_id": ..., "title": ...},
#                {"input": "https://www.youtube.com/watch?v=...", "status": "added", "source_id": ..., "title": ...}]}
```

Batch mode is URL-only (a non-URL entry is reported as a per-item `VALIDATION`
error, never added as text); `source_type`/`url`/`text`/`title`/`path`/
`document_id`/`mime_type` are not valid with `urls`, but `allow_internal`
applies to every entry.

### Content-sanity warnings on ready web pages

A dead link, [soft-404](https://en.wikipedia.org/wiki/HTTP_404#Soft_404), or
paywalled page frequently ingests as a **READY** source with little-to-no
extractable text — a "ghost source" that add-time status can't catch because a
soft-404 serves HTTP 200. `source_wait` — and batch `source_add(urls=[...])` for
an item that is *already* READY the moment it returns (single-mode `source_add`
adds asynchronously, so it never runs this check) — attaches a non-blocking,
advisory `warning` to such a source. The check is **best-effort and never
rejects**: the source stays READY, `ok` stays `true`, and any fetch failure
(including a >5s slow `source_read`) degrades to no warning rather than breaking
the wait.

It fires on a **web-page source only** (`kind == "web_page"`) via two body-only
signals — the title is never scanned:

| Signal | Threshold | Warning contains |
|--------|-----------|------------------|
| **char-thin** | indexed text shorter than **100 characters** | `"little/no text extracted (N chars) …"` |
| **dead-link boilerplate** | **indexed text** shorter than **2000 characters** that (casefolded) contains any of the phrases below | `"ingested as ready (N chars) but the body matches a dead-link / error-page pattern …"` |

The full dead-link phrase set (the complete list, so you can build a fixture that
trips it): `broken link`, `page not found`, `page isn't available`, `page does
not exist`, `page no longer available`, `no longer available`, `error 404`, `404
not found`, `whoops!`.

Both gates measure the source's **indexed text** length (`char_count` from a
`source_read` with `detail="full"`), not the raw HTTP response — a large HTML
page that indexes to little text is still caught. The 2000-char gate is what
keeps the weaker phrases safe: a page whose indexed text is 2000 chars or longer
is never phrase-scanned (so `broken link` in a real article about broken links,
or a shop's `no longer available`, does not false-positive), and the phrases are
all multi-word / anchored — no bare `404` or `not found`. Every warning ends with
`verify with source_read (detail="full").` (trailing period included).

**To exercise the warning branch** (the reason this is documented): note that a
`text` source — even an empty one — is *never* flagged; only a `web_page` under
the thresholds above is. So the reliable trigger is a URL that resolves to a
near-empty or soft-404 page. To unit-test your own handling of the branch
without a live URL, mock the source's fetched body under the threshold and assert
the warning shape — copy the pattern from
[`tests/unit/mcp/test_sources.py`](../tests/unit/mcp/test_sources.py) (see
`test_source_wait_thin_web_page_warns`, `test_source_wait_soft_404_body_phrase_warns`,
and the `_THIN_SOURCE_CHAR_THRESHOLD` boundary test).

### Generate and download a studio artifact

```text
task = studio_generate(notebook="Quantum Computing", artifact_type="audio")
studio_status(notebook="Quantum Computing", task_id="<task_id from above>")   # poll until complete
studio_download(notebook="Quantum Computing", artifact_type="audio", path="podcast.mp3")

# Target a specific/older artifact instead of the latest-by-type (full ID or unique prefix):
studio_download(notebook="Quantum Computing", artifact_type="audio", path="old_podcast.mp3", artifact_id="aaaaaaaa-aaaa")

# Per-kind styling options are agent-settable, e.g. a custom-styled video:
studio_generate(notebook="Quantum Computing", artifact_type="video",
                  style="custom", style_prompt="hand-drawn diagrams")
```

`artifact_type` is one of `audio`, `video`, `cinematic-video`, `slide-deck`, `quiz`, `flashcards`,
`infographic`, `data-table`, `mind-map`, `report`. Each kind's styling options are agent-settable
(matching the CLI flags): `audio_format` / `audio_length` (audio); `video_format` / `style` /
`style_prompt` (video — `style` / `style_prompt` are rejected for `video_format` `cinematic` and
`short`, which use a fixed visual style); `deck_format` / `deck_length` (slide-deck); `quantity` / `difficulty`
(quiz, flashcards); `orientation` / `detail` / `style` (infographic); `map_kind` (mind-map);
and `report_format` (report). `cinematic-video` and `data-table` take no per-kind options. An
option is valid only for its own kind — passing one to a different `artifact_type` is a
validation error, not a silent no-op.

### Run deep research and import the findings

```text
task = research_start(notebook="Quantum Computing", query="post-quantum cryptography", source="web", mode="deep")
research_status(notebook="Quantum Computing", poll_task_id=task["poll_task_id"])
research_import(notebook="Quantum Computing", poll_task_id=task["poll_task_id"])
```

`source` is `web` or `drive`; `mode` is `fast` or `deep`. Pass the
`poll_task_id` returned by `research_start` — under the **same** parameter name,
`poll_task_id` — when polling, importing, or cancelling, so the value copies
verbatim from one tool's output into the next and the request is pinned to the
intended research task (for a **deep** run it is the `report_id`; the raw
`task_id` is an unpollable sessionId). Omitting the pin on `research_status` is
allowed only when the notebook has a single in-flight task. `research_status`
omits the large report by default — pass `include_report=true` to fetch it once
`completed`.

> **Deprecated (removed in v0.9.0):** `research_status`/`research_import` also
> accept the old `task_id` name and `research_cancel` the old `run_id` name as
> aliases for `poll_task_id`. Passing an alias still works but emits a
> `DeprecationWarning` and adds a `deprecation` note to the result — switch to
> `poll_task_id`. See [docs/deprecations.md](deprecations.md).

## Tool reference

| Domain | Tools |
|--------|-------|
| **Notebooks** | `notebook_list(limit?, offset?)` · `notebook_create(title)` · `notebook_describe(notebook, include_metadata?)` (AI description; `include_metadata=true` adds a `metadata` block with notebook details + source list) · `notebook_rename(notebook, new_title)` · `notebook_delete(notebook, confirm)` |
| **Sources** | `source_list(notebook, status?, label?, detail?, limit?, offset?)` (each source has string `kind`/`status_label`; `status` filters to one of ready\|processing\|error\|preparing — e.g. `status="error"` finds failed imports; `detail=compact` returns a low-token roster of just `id`/`title`/`kind`/`status_label`/`created_at`) · `source_read(notebook, source, detail?, output_format?, max_chars?, offset?)` (`detail=full` (default) → metadata + a bounded slice of the indexed text: `max_chars` caps `content` (default 10k), `offset` pages, plus a `truncated` flag and the full `char_count`; `detail=summary` → low-token triage: AI summary **+ keywords**, not the body; `output_format`: text\|markdown) · `source_rename(notebook, source, new_title)` · `source_delete(notebook, source, confirm)` · `source_wait(notebook, source?, timeout, interval)` (a READY web page with thin/empty text, or a short body matching a dead-link / soft-404 boilerplate pattern, carries a non-blocking `warning`) · `source_add(notebook, source_type, ..., allow_internal?)` (single; echoes `kind`/`status_label`, flags a failed import inline with a `warning`) / `source_add(notebook, urls=[...], allow_internal?)` (batch → per-item `results`; a synchronously-ready web-page item may also carry the same content-sanity `warning`) · `source_add_and_wait(notebook, source_type, ..., timeout?, interval?)` (single-mode `source_add` + `source_wait` in one call → the `source_wait` aggregate plus a top-level `source_id`; not for batch or a remote `file` upload) |
| **Chat** | `chat_ask(notebook, question?, conversation_id?, references?, source_ids?, history?, suggest_followups?)` (`references`: lite\|full; never returns the raw debug blob; `source_ids` scopes to specific sources — list, JSON-array string, or comma string; omit for all; `history`>0 also returns up to N prior `{question, answer}` pairs — omit `question` to recall only; `suggest_followups=true` also returns `suggested_prompts` (3 questions to ask — works question-less too)) · `chat_configure(notebook, chat_mode?, goal?, response_length?)` (`chat_mode`: default\|learning-guide\|concise\|detailed — a preset, mutually exclusive with `goal`/`response_length`; a **partial** custom call sets just `goal` or just `response_length` and **merges** with the current settings — the omitted field is preserved, not reset; only a bare call, no preset and neither field, is rejected) · `suggest_prompts(notebook, surface?, source_ids?, query?)` (READ_ONLY; `surface`: ask\|audio-deep-dive\|audio-brief\|audio-critique\|audio-debate\|video-explainer\|video-short\|quiz\|flashcards — returns `{title, prompt}` suggestions to steer that studio surface; `ask` (default) = chat questions) |
| **Notes** | `note_save(notebook, note?, title?, content?)` (upsert: omit `note` to **create** — `title` AND `content` required; pass a `note` ref to **update** — `title` and/or `content`, title-only = rename). Reading and deleting notes fold into the Studio row below. |
| **Studio** | `studio_list(notebook, item?, kind?, detail?, limit?, offset?)` (the unified Studio panel — **notes AND artifacts** merged into one `items` list; each item has `id`/`title`/`type` where `type` is `note` or a hyphenated artifact kind; artifacts add `status_label`/`url`; `detail=summary` (default) gives each note a bounded `content_preview` + full-body `char_count` to keep a discovery listing low-token, `detail=full` returns the whole note `content`, `detail=compact` collapses every item to `id`/`title`/`type`/`status_label`/`created_at`; `kind` filters to one `type`; `item` fetches one note-or-artifact by ref as a 1-element list, always with the note's full `content`) · `studio_generate(notebook, artifact_type, …)` · `studio_status(notebook, task_id)` · `studio_get_prompt(notebook, artifact)` (the free-text prompt an artifact was generated from; `null` if none) · `studio_download(notebook, artifact? \| artifact_type?, path?, output_format?, artifact_id?)` (target by `artifact` name-or-id ref **or** by `artifact_type` [+ `artifact_id` for a specific one, else latest]) · `studio_rename(notebook, item, new_title)` (cross-type: renames a note OR an artifact resolved from the merged list) · `studio_retry(notebook, artifact)` (re-run a failed artifact in place; task_id == artifact_id) · `studio_delete(notebook, item, confirm)` (cross-type: deletes a note OR an artifact resolved from the merged list) |
| **Research** | `research_start(notebook, query, source, mode)` (returns `poll_task_id` — the one id status/import/cancel drive off) · `research_status(notebook, poll_task_id?, include_report?, report_max_chars?, source_limit?, source_offset?)` (report + per-source `report_markdown` omitted unless `include_report`) · `research_import(notebook, poll_task_id)` · `research_cancel(notebook, poll_task_id)` (sends the cancel unless the run is already terminal → `cancel_requested`). The old `task_id` / `run_id` param names are deprecated aliases for `poll_task_id`, removed in v0.9.0 |
| **Sharing** | `share_status(notebook)` (is_public/access/share_url/shared_users; enums as string labels; `view_level` omitted — the read API can't report it) · `share_set_access(notebook, public?, view_level?, confirm)` (link settings; `view_level`: full\|chat, echoed back only when set; `confirm` gates public widening restricted→public) · `share_set_user(notebook, email, permission?, notify?, message?, confirm)` (upsert grant; `permission`: editor\|viewer; `notify` defaults `false`; `confirm` gates every grant) · `share_remove_user(notebook, email, confirm)` |
| **Server** | `server_info(include_account?)` — version + local auth health; `include_account=true` adds an `account` block: signed-in identity (`email`, `authuser`) plus notebook/source limits and global `output_language` for quota pacing + language context (best-effort; identity is network-free from the profile, the quota fields need a live session). `email` is real account PII, returned only under this opt-in flag |

Tools that only read are annotated read-only; the destructive tools (the three `*_delete` tools plus `share_remove_user`) are annotated destructive
and require `confirm`. A host that honors MCP annotations can auto-allow the read-only calls and
gate the destructive ones.

## Troubleshooting

- **`AUTH` errors / "not authenticated".** Run `notebooklm login` (or `notebooklm -p <profile> login`)
  in a terminal, then restart the server. Check with the `server_info` tool, which reports auth health.
- **`uvx` / `uv` not found.** Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux)
  or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows). The desktop launcher also
  searches common install dirs beyond `PATH`.
- **Client doesn't see the tools.** Confirm the config was written (`notebooklm mcp install <client>`)
  and **restart the client** — most hosts only read MCP config at startup.
- **Wrong account.** The server binds one profile per process. Start it with `--profile <name>`, or set
  `NOTEBOOKLM_PROFILE`. See [configuration.md](configuration.md#multiple-accounts).
- **`RATE_LIMITED`.** NotebookLM enforces per-account quotas; the error is `retriable=true` — back off
  and retry.

## See also

- [installation.md](installation.md#running-the-mcp-server-mcp-extra) — the `mcp` extra + run/connect summary
- [`desktop-extension/README.md`](../desktop-extension/README.md) — one-click Claude Desktop `.mcpb` bundle (prebuilt, attached to each stable release)
- [configuration.md](configuration.md) — profiles, multi-account, storage
- [cli-reference.md](cli-reference.md) — the equivalent CLI commands
