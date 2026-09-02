---
name: notebooklm
description: Operate Google Gemini Notebook through notebooklm-py to create notebooks, add sources, chat over content, run research, generate and download artifacts, and use features beyond the web UI. Use when the user names Google Gemini Notebook or asks to use it for podcasts, videos, reports, quizzes, flashcards, slide decks, infographics, mind maps, or source-grounded summaries.
---

# Google Gemini Notebook Automation

Use the `notebooklm` CLI for agent workflows. Prefer `--json` and explicit IDs so every
operation is inspectable and safe under concurrency. Use the typed async Python API only when the
user requests application code or the CLI cannot express the workflow.

## Setup and Authentication

Install the package in the user's existing environment; do not create a separate environment unless
requested:

```bash
pip install "notebooklm-py[browser]"
pip install "notebooklm-py[cookies]"  # optional browser-cookie extraction
```

In a resettable headless sandbox that reuses a host-generated `storage_state.json` or
`NOTEBOOKLM_AUTH_JSON`, the base `pip install notebooklm-py` is sufficient. Reusing a
`master_token.json` requires `pip install "notebooklm-py[headless]"`; `[browser]` is needed for
interactive login, and `[cookies]` is needed for browser-cookie extraction.

Use PyPI or a release tag, not an unreleased `main` checkout. For the extras matrix, headless setup,
CI secrets, skill installation, or the opt-in Android backend, read the
[installation guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/installation.md).

Before a workflow, verify real authentication rather than merely parsing the cookie file:

```bash
notebooklm auth check --test --json
```

Require both `"status": "ok"` and `"checks.token_fetch": true`. If validation fails:

- With a display, run `notebooklm login` and validate again.
- In a headless environment, install `[cookies]` and use
  `notebooklm login --browser-cookies <browser>`. Use
  `notebooklm auth inspect --browser <browser>` first when account selection is unclear.
- If previously valid cookies became stale, try `notebooklm auth refresh`; use
  `notebooklm auth refresh --browser-cookies <browser>` after signing back into the browser.

`notebooklm status` reports selected-notebook context, not authentication.

Treat `storage_state.json`, `master_token.json`, and `NOTEBOOKLM_AUTH_JSON` as bearer credentials:
never print, log, or commit them. Prefer a secret store in CI.

## Operating Invariants

1. Use `--json` for discovery and mutations, then retain the returned full UUIDs. Important
   envelopes are `.notebook.id` from `create`, `.source.id` from `source add`, and `.task_id` from
   asynchronous generators. `generate mind-map` instead returns `mind_map`, `note_id`, and `kind`;
   it has no task ID or separate wait step.
2. Pass `-n/--notebook <id>` on every notebook-scoped command in automation or concurrent work.
   Do not rely on `notebooklm use`. For every concurrent run, also set a unique
   `NOTEBOOKLM_PROFILE=agent-<id>` so authentication recovery and other profile writes are isolated.
3. After adding sources, wait for every captured source ID before chat or generation. Source JSON
   states are lowercase: require `status == "ready"`; stop on `"error"`.
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

After confirmation, pass `--yes`/`-y` where supported. Do not assume `--json` bypasses destructive
confirmation.

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

Common operations:

| Goal | Command |
|---|---|
| List or create notebooks | `notebooklm list --json`; `notebooklm create "Title" --json` |
| Add and wait for a source | `notebooklm source add <input> -n <nb> --json`; `notebooklm source wait <src> -n <nb>` |
| Chat | `notebooklm ask "question" -n <nb> --json` |
| Research | `notebooklm source add-research "query" -n <nb> --mode fast --json` (`deep` is also supported) |
| List or wait for artifacts | `notebooklm artifact list -n <nb> --json`; `notebooklm artifact wait <id> -n <nb>` |
| Generate | `notebooklm generate <type> ... -n <nb> --json` |
| Download | `notebooklm download <type> <path> -n <nb> -a <artifact>` |

For the full surface, consult the installed command help or the
[CLI reference](https://github.com/teng-lin/notebooklm-py/blob/main/docs/cli-reference.md).
For application code, read the
[Python API guide](https://github.com/teng-lin/notebooklm-py/blob/main/docs/python-api.md).

## Canonical Source-to-Artifact Workflow

Keep `{notebook_id}`, every `{source_id}`, and `{artifact_id}` from JSON output:

1. `notebooklm create "Research: topic" --json`
2. `notebooklm source add <input> -n {notebook_id} --json` for each input.
3. After confirmation for a foreground wait, run
   `notebooklm source wait {source_id} -n {notebook_id} --timeout 600` for every captured source.
4. After confirmation, generate the requested type. For audio:
   `notebooklm generate audio "instructions" -n {notebook_id} -s {source_id} --json`.
   Repeat `-s` for each selected source and capture `.task_id` as `{artifact_id}`.
5. After confirmation for a foreground wait, run
   `notebooklm artifact wait {artifact_id} -n {notebook_id} --timeout 1200`.
6. After confirmation to write the file, run
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

Retain imported source IDs and wait for readiness before later chat or generation.

## Generation Notes

Available generators include `audio`, `video`, `slide-deck`, `infographic`, `report`, `mind-map`,
`data-table`, `quiz`, and `flashcards`. Inspect `notebooklm generate <type> --help` because formats,
styles, source selection, language, and retry support vary by type.

Keep these non-obvious distinctions:

- Mind map (`--kind interactive`, default) is an asynchronous studio artifact.
- Mind map (`--kind note-backed`) is synchronous and supports `--instructions`.
- `generate video --format cinematic` ignores `--style`, requires Google AI Ultra, and can take
  roughly 30-40 minutes.
- Slide-deck has no orientation flag. Request portrait output in the description (for example,
  `9:16 portrait`); slide revision cannot change the deck's orientation.
- For a custom report, pass the prompt as the positional description with `--format custom`;
  `--append` applies only to built-in report formats.

For prompts too long or awkward for shell quoting, use `--prompt-file PATH` on `ask`, research, and
supported generators. It contains prompt text; upload source documents with `source add` instead.

## Output and Citations

Use JSON structurally rather than parsing human output. Common lifecycle values are:

- sources: `processing` -> `ready` or `error`;
- artifacts: `pending`/`in_progress` -> `completed`.

Chat JSON includes `answer`, `conversation_id`, and `references[].source_id`. Citation offsets refer
to Google Gemini Notebook's internal chunks, not raw fulltext offsets. In Python, retrieve source
fulltext and use `SourceFulltext.find_citation_context()` when exact surrounding text is needed.

## Failure Handling

On failure, run safe read-only diagnosis first:

```bash
notebooklm auth check --test --json
notebooklm list --json
notebooklm source list -n {notebook_id} --json
notebooklm artifact list -n {notebook_id} --json
notebooklm research status -n {notebook_id} --run-id {research_run_id} --json
```

Inspect only the commands relevant to the failed workflow. Do not mutate state during diagnosis.

- Exit 0 means success. Expected command failures use exit 1. A `source wait` timeout uses exit 2;
  `artifact wait` and `research wait` timeouts use exit 1.
- On auth failure, revalidate `checks.token_fetch`; log in only if it is not `true`.
- On a wait timeout, report it and inspect the exact source, artifact, or research run.
- Generation is rate-limited by Google. Preserve the task ID, inspect status, and retry only when the
  user authorizes it; do not loop indefinitely.
- On a protocol error, check the installed version and the project's issue tracker before suggesting
  a workaround.

Keep progress updates brief and include the relevant returned ID. Never expose credential contents.

## Skill Installation

If this file is already inside an agent skill directory, the skill itself is installed. Otherwise:

- `notebooklm skill install` installs or updates supported local skill targets.
- `notebooklm skill package` builds an uploadable archive for sandboxed agent environments.
