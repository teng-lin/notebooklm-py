---
name: notebooklm
description: Install, authenticate, troubleshoot, and operate Gemini Notebook through the notebooklm-py CLI or typed async Python API. Use for notebook and source management, grounded chat and research, and artifact generation or download when the user mentions Gemini Notebook, notebooklm-py, the notebooklm CLI, or its Python API. Do not use for the generic Gemini API or unrelated content creation.
---

# Gemini Notebook Automation

Use the `notebooklm` CLI for agent workflows. Prefer `--json` and explicit IDs so every
operation is inspectable and safe under concurrency. Use the typed async Python API only when the
user requests application code or the CLI cannot express the workflow. The readiness, identity,
authorization, and credential-handling rules below apply to both interfaces.

## Setup and Authentication

Requires Python 3.10+. Install the package in the user's existing environment; do not create a
separate environment unless requested:

```bash
pip install "notebooklm-py[browser]"
pip install "notebooklm-py[cookies]"  # optional browser-cookie extraction
```

If system `pip` reports `externally-managed-environment`, do not use `--break-system-packages`.
For CLI-only use, offer `uv tool install "notebooklm-py[browser]"` or the equivalent `pipx`
command; for application code, use the user's active project environment.

For unattended or headless work, prefer durable profile-backed master-token auth over a copied
cookie snapshot. Install `pip install "notebooklm-py[headless]"`; the one-time automatic OAuth
capture also needs `[browser]`. On a trusted workstation run
`notebooklm login --master-token --account <email>`, then deploy `master_token.json`, not
`storage_state.json`, to the selected profile. `NOTEBOOKLM_HOME` selects the private base directory
and `NOTEBOOKLM_PROFILE` selects its profile; defaults resolve to
`~/.notebooklm/profiles/default/master_token.json`.

In CI, `NOTEBOOKLM_MASTER_TOKEN_JSON` is a secret-transport convention, not an environment variable
the package reads directly. Write its exact value to the selected profile's `master_token.json`
with mode `0600`, unset it, then run `notebooklm auth refresh` to mint `storage_state.json`. A
sibling master token can automatically re-mint expired file-backed cookies. Inline
`NOTEBOOKLM_AUTH_JSON` is only a short-lived fallback; it bypasses this recovery path.

Use PyPI or a release tag, not an unreleased `main` checkout. When available, consult the
[installation guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md).

Before a workflow, verify real authentication rather than merely parsing the cookie file:

```bash
notebooklm auth check --test --json
```

Require `.status == "ok"` and `.checks.token_fetch == true`. If validation fails:

- With a display, run `notebooklm login` and validate again.
- In a headless environment, install `[cookies]` and use
  `notebooklm login --browser-cookies <browser>`. Use
  `notebooklm auth inspect --browser <browser>` first when account selection is unclear.
- If previously valid cookies became stale, try `notebooklm auth refresh`; use
  `notebooklm auth refresh --browser-cookies <browser>` after signing back into the browser.

The normal `--test` preflight may heal and persist refreshed cookies. Add `--passive` when the
check must be strictly read-only, including the failure-diagnosis workflow below.

`notebooklm status` reports selected-notebook context, not authentication.

Treat both auth files as bearer credentials: never print, log, or commit them. A master token is a
durable full-account credential that survives password changes; use a dedicated account, protect
it in a secret store and as `0600` on disk, and explicitly revoke it if exposed.

## Operating Invariants

1. Use `--json` for discovery and mutations, then retain the returned full UUIDs. Important
   envelopes are `.notebook.id` from `create`, `.source.id` from `source add`, and `.task_id` from
   asynchronous generators. `generate mind-map` instead returns `mind_map`, `note_id`, and `kind`;
   both kinds return a finished result with no task ID or separate `artifact wait` step.
2. Pass `-n/--notebook <id>` on every notebook-scoped command in automation or concurrent work.
   Do not rely on `notebooklm use`. For every concurrent run, also set a unique
   `NOTEBOOKLM_PROFILE=agent-<id>` so context and profile writes are isolated. A new profile has no
   credentials: put a `master_token.json` copy in that profile and mint its storage before use.
   Never share one writable `storage_state.json` across agents.
3. After adding sources, retain every `.source.id`, then run `source wait` for each before chat or
   generation. The add envelope has no status. Require wait exit 0 and `status == "ready"`; let the
   waiter handle media-specific transient `error` rows.
4. After an asynchronous generator returns a task/artifact ID, pass it positionally to
   `artifact wait` with `-n <notebook_id>`. Download that exact artifact with
   `-a <artifact_id> -n <notebook_id>`; never select the latest visible artifact. Mind-map generation
   returns its completed result directly and does not need `artifact wait`.
5. For overlapping research runs, always pass `--run-id <research_run_id>`.
6. Use a host's background facility only when it actually exists. Keep wait and dependent download
   commands in one sequential job, and download only after the wait exits 0. Otherwise run in the
   foreground or return exact ID-pinned commands to the user.

## Authorization Boundaries

Safe inspection and explicitly requested creation, source addition, chat, and prompt suggestion can
run directly. Diagnose failures with read-only commands before attempting recovery.

Obtain confirmation immediately before an action when it was not already clearly authorized:

- destructive commands such as notebook/source/note/artifact/label/profile deletion, sharing
  removal, logout, clear, research cancellation, and `ask --new`;
- `language set`, because the default mode changes the account-global output language (prefer a
  generation command's `--language` override);
- generation or long foreground waits, which can take minutes and be rate-limited;
- downloads, which write files;
- `research wait --import-all`, which imports sources;
- `ask --save-as-note` and `history --save`, which create notes.

User intent, not the presence of a CLI prompt, is the authorization boundary. After authorization,
pass `--yes`/`-y` where supported. Most destructive JSON commands refuse to prompt without it, but
some, including `ask --new --json` and `share remove --json`, execute without prompting. Never
treat prompt absence as consent.

`research cancel` is fire-and-forget. After an authorized cancellation, verify the exact run with
`notebooklm research status -n <notebook_id> --run-id <research_run_id> --json`.

## Command Discovery

Use the installed CLI's help as the version-matched source of truth instead of guessing flags:

```bash
notebooklm --help
notebooklm source --help
notebooklm research --help
notebooklm generate --help
notebooklm artifact --help
notebooklm download --help
```

Also inspect `notebooklm --version` and drill down to the exact command, such as
`notebooklm generate audio --help`, whenever its help differs from this skill.

Common operations:

| Goal | Command |
|---|---|
| Check compute usage | `notebooklm usage --json`; `notebooklm usage --categories` for category availability and estimated costs |
| List or create notebooks | `notebooklm list --json`; `notebooklm create "Title" --json` |
| Add and wait for a source | `notebooklm source add <input> -n <nb> --json`; `notebooklm source wait <src> -n <nb>` |
| Chat | `notebooklm ask "question" -n <nb> --json` |
| Research | `notebooklm source add-research "query" -n <nb> --mode fast --json` (`deep` is also supported) |
| List or wait for artifacts | `notebooklm artifact list -n <nb> --json`; `notebooklm artifact wait <id> -n <nb>` |
| Generate | `notebooklm generate <type> ... -n <nb> --json` |
| Download | `notebooklm download <type> <path> -n <nb> -a <artifact>` |

For the full surface, consult the installed command help or, when available, the
[CLI reference](https://github.com/teng-lin/notebooklm-py/blob/main/docs/cli-reference.md).
For application code, use the baseline below and, when available, the
[Python API guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/python-api.md).

## Canonical Source-to-Artifact Workflow

Keep `{notebook_id}`, every `{source_id}`, and `{artifact_id}` from JSON output:

An explicit request for this completed workflow authorizes its normal prerequisite waits, requested
generation, and requested output file. Confirm only work not already authorized by that request.

1. `notebooklm create "Research: topic" --json`
2. `notebooklm source add <input> -n {notebook_id} --json` for each input.
3. Once the foreground wait is authorized, run
   `notebooklm source wait {source_id} -n {notebook_id} --timeout 600` for every captured source.
4. Once generation is authorized, generate the requested type. For audio:
   `notebooklm generate audio "instructions" -n {notebook_id} -s {source_id} --json`.
   Repeat `-s` for each selected source and capture `.task_id` as `{artifact_id}`.
5. Once the foreground wait is authorized, run
   `notebooklm artifact wait {artifact_id} -n {notebook_id} --timeout 1200`.
6. Once the output write is authorized, run
   `notebooklm download audio ./podcast.m4a -a {artifact_id} -n {notebook_id}`.

For analysis without generation, replace steps 4-6 with an ID-pinned chat command only after every
source is ready:

```bash
notebooklm ask "Summarize the key arguments" -n {notebook_id} --json
```

## Deep Research

Deep research can take 15-30+ minutes. Start it non-blocking and retain
`.poll_task_id // .task_id` as `{research_run_id}`:

```bash
notebooklm source add-research "query" -n {notebook_id} --mode deep --no-wait --json
```

Import only after explicit authorization, pinning both IDs:

```bash
notebooklm research wait -n {notebook_id} --run-id {research_run_id} \
  --import-all --timeout 1800 --json
```

With `--import-all`, `--timeout` is a per-phase budget for polling and import retry, so this example
can consume roughly 3600 seconds of host wall time.

Retain newly created source IDs from `.imported_sources[].id` and wait for readiness before later
chat or generation.

## Python API Baseline

When the full API guide is unavailable, use the installed typed API and its docstrings; do not guess
method names. Keep the same IDs and readiness gates as the CLI workflow:

```python
import asyncio

from notebooklm import NotebookLMClient


async def main(url: str) -> None:
    async with NotebookLMClient.from_storage() as client:
        notebook = await client.notebooks.create("Research: topic")
        source = await client.sources.add_url(notebook.id, url)
        await client.sources.wait_until_ready(notebook.id, source.id, timeout=600)

        answer = await client.chat.ask(
            notebook.id, "Summarize the key arguments", source_ids=[source.id]
        )
        print(answer.answer)

        task = await client.artifacts.generate_audio(
            notebook.id,
            source_ids=[source.id],
            instructions="Focus on the key arguments",
        )
        final = await client.artifacts.wait_for_completion(
            notebook.id, task.task_id, timeout=1200
        )
        if not final.is_complete:
            raise RuntimeError(f"Generation ended with {final.status}: {final.error}")
        await client.artifacts.download_audio(
            notebook.id, "./podcast.m4a", artifact_id=task.task_id
        )


asyncio.run(main("https://example.com"))
```

`NotebookLMClient.from_storage()` is an async context manager and is not awaited. A client is
re-entrant on one event loop but is not thread-safe; create one client per loop. Public namespaces
include `notebooks`, `sources`, `chat`, `research`, `artifacts`, `mind_maps`, `notes`, `settings`,
`sharing`, `labels`, and `collections`. Apply the authorization boundaries above before running
state-changing, long-running, or file-writing calls.

## Generation Notes

Available generators include `audio`, `video`, `slide-deck`, `infographic`, `report`, `mind-map`,
`data-table`, `quiz`, and `flashcards`. Inspect `notebooklm generate <type> --help` because formats,
styles, source selection, language, and retry support vary by type.

Keep these non-obvious distinctions:

- Mind map (`--kind interactive`, default) is an asynchronous studio artifact internally, but the
  CLI polls it to completion and returns `{mind_map, note_id, kind}`; do not run `artifact wait`.
- Mind map (`--kind note-backed`) is server-synchronous. Both kinds accept `--instructions`;
  interactive applies it reliably, while the server may ignore it for note-backed maps.
- `generate video --format cinematic` ignores `--style`, requires Google AI Ultra, and can take
  roughly 30-40 minutes.
- Slide-deck has no orientation flag. Request portrait output in the description (for example,
  `9:16 portrait`); slide revision cannot change the deck's orientation.
- For a custom report, pass the prompt as the positional description with `--format custom`;
  `--append` applies only to built-in report formats.

For prompts too long or awkward for shell quoting, use `--prompt-file PATH` on `ask`,
`source add-research`, and supported generators. It contains prompt text; upload source documents
with `source add` instead.

## Output and Citations

Use JSON structurally rather than parsing human output. Common lifecycle values are:

- sources: `unknown`/`preparing`/`processing` -> `ready` or `error`; proceed only on `ready`;
- artifacts: `pending`/`in_progress` -> `completed`, `failed`, or `removed`; `not_found` may be a
  brief listing lag. Proceed or download only on `completed`.

Chat JSON includes `answer`, `conversation_id`, and `references[].source_id`. A reference's
`start_char`/`end_char` are UTF-16 offsets into the structured source document, not flat
`SourceFulltext.content`. In Python, use
`from notebooklm import resolve_chat_reference_passage`, then call
`await resolve_chat_reference_passage(client, notebook_id, reference)`; it uses the exact document
range and falls back to `find_citation_context()` when necessary.

## Failure Handling

On failure, run safe read-only diagnosis first:

```bash
notebooklm auth check --test --passive --json
notebooklm list --json
notebooklm source list -n {notebook_id} --json
notebooklm artifact list -n {notebook_id} --json
notebooklm research status -n {notebook_id} --run-id {research_run_id} --json
```

Inspect only the commands relevant to the failed workflow. Do not mutate state during diagnosis.

- Exit 0 means success. Expected command failures use exit 1. A `source wait` timeout uses exit 2;
  `artifact wait` and `research wait` timeouts use exit 1.
- Branch on exit code first. Ordinary handled JSON failures use `{error, code, message}`, while wait
  commands return domain envelopes such as `{"status": "timeout", "error": "..."}`.
- On auth failure, revalidate `checks.token_fetch`; log in only if it is not `true`.
- On a wait timeout, report it and inspect the exact source, artifact, or research run.
- Generation is rate-limited by Google. Preserve the task ID, inspect status, and retry only when the
  user authorizes it; do not loop indefinitely. For an existing failed Studio artifact, inspect
  `notebooklm artifact retry <artifact_id> -n {notebook_id} --help` before retrying in place.
- On a protocol error, record the installed version, exact command, relevant IDs, and redacted
  error. Check the project's issue tracker when network access exists; do not invent a workaround
  when it does not.

Keep progress updates brief and include the relevant returned ID. Never expose credential contents.

## Skill Installation

If this file is already inside an agent skill directory, the skill itself is installed. Otherwise:

- `notebooklm skill install` installs or updates supported local skill targets.
- `notebooklm skill package` builds an uploadable archive for sandboxed agent environments.
- `notebooklm skill status --json` reports installed versions and `content_mismatch`.
