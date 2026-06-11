# CLI Exit-Code Convention

**Status:** Active
**Last Updated:** 2026-06-11

This document defines the exit-code policy for the `notebooklm` CLI. Shell
scripts, CI pipelines, and AI-agent automations should rely on these codes for
control flow rather than scraping stdout/stderr text — the text is intended for
humans and may evolve, but the exit-code contract is stable.

The companion architectural decision for the `--json` error contract is
[ADR-0015](adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md);
this document is the surface-level reference for callers, and ADR-0015 is the
rationale for the post-parse `ClickException` rules called out below.

For the canonical implementation, see the `handle_errors` context manager in
[`src/notebooklm/cli/error_handler.py`](../src/notebooklm/cli/error_handler.py)
— the policy table lives in its docstring and the `KeyboardInterrupt` clause
sits immediately below (at the time of writing, around lines 64-67 and :81;
rely on the symbol names rather than the line numbers if they drift).

## Standard exit codes

| Code | Meaning | When you'll see it |
|------|---------|-------------------|
| `0`  | Success | The command completed and produced its intended effect. |
| `1`  | User / application error | Validation, authentication, rate limiting, network failure, configuration error, or any `NotebookLMError` raised by the library. |
| `2`  | System / unexpected error | Unhandled exception (likely a bug). The CLI suggests reporting at the issue tracker. Also used for the `source wait` timeout (see exceptions below). |
| `130`| Cancelled by user | The process received `SIGINT` (Ctrl-C). `130 = 128 + signal 2`, the conventional shell value for SIGINT-terminated processes. |

The policy comment in `error_handler.py` is the source of truth:

```text
Exit codes:
    1: User/application error (validation, auth, rate limit, etc.)
    2: System/unexpected error (bugs, unhandled exceptions)
    130: Keyboard interrupt (128 + signal 2)
```

<a id="exception-exit-code-mapping"></a>

## Exception → exit-code mapping

The `handle_errors` context manager wrapping every CLI command translates
library exceptions into exit codes. The table below summarises the live
mapping in `error_handler.py`:

| Library exception | JSON `code` | Exit |
|---|---|---|
| `RateLimitError`        | `RATE_LIMITED`      | `1` |
| `AuthError`             | `AUTH_ERROR`        | `1` |
| `ValidationError`       | `VALIDATION_ERROR`  | `1` |
| `ConfigurationError`    | `CONFIG_ERROR`      | `1` |
| `NetworkError`          | `NETWORK_ERROR`     | `1` |
| `NotebookLimitError`    | `NOTEBOOK_LIMIT`    | `1` |
| `NotebookLMError` (other) | `NOTEBOOKLM_ERROR` | `1` |
| `KeyboardInterrupt`     | `CANCELLED`         | `130` |
| Anything else (`Exception`) | `UNEXPECTED_ERROR` | `2` |
| Parse-time `click.UsageError` / `click.BadParameter` (Click's parser, before command body runs) | `VALIDATION_ERROR` under `--json`; — in text mode | `2` (under `--json`: typed JSON envelope on stdout, **exit code preserved**; text mode: Click's `Usage:/Error:` on stderr) |
| Parse-time `click.ClickException` (other subclasses raised by Click's parser) | `VALIDATION_ERROR` under `--json`; — in text mode | `1` (same: JSON envelope under `--json` with the exit code preserved; native text otherwise) |
| Post-parse `ClickException` raised from a command body or service module | `VALIDATION_ERROR` (or another standard code, per the raise site) | `1` (typed JSON envelope under `--json`; see ADR-0015) |

`click.ClickException` raised by **Click's own parser** is the *parse-time*
path: argv parsing decides the command body should not run at all (unknown
flag, type-validation failure, missing required argument), so `handle_errors(...)`
— the per-command context manager — is never on the stack. But the **root
group** (`SectionedGroup.main` in
[`src/notebooklm/cli/grouped.py`](../src/notebooklm/cli/grouped.py)) sits
*above* `handle_errors`: it runs Click's superclass in non-standalone mode and
catches the parse-time `ClickException` itself.

- **Under `--json`**, the root group emits the typed JSON error envelope on
  stdout (`{ "error": true, "code": "VALIDATION_ERROR", "message": ... }`,
  empty stderr), so automation that passed `--json` still gets a parseable
  document for argv-level failures. The **exit code is preserved** from Click —
  `2` for `UsageError` / `BadParameter`, `1` for the base `ClickException` and
  other non-usage subclasses. (Note the envelope `code` is `VALIDATION_ERROR`
  regardless of which Click subclass fired; only the exit code carries the
  `2`-vs-`1` distinction.)
- **In text mode** (no `--json`), behavior is unchanged: Click renders its own
  `Usage: ... / Error: ...` message on stderr and exits with that same
  `exc.exit_code` (`2` / `1`).

This supersedes the original ADR-0015 §1 stance that "no JSON envelope is
emitted at parse time"; see the amendment note in
[ADR-0015](adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md).

`ClickException`-subclass failures raised from inside a **command body or
its service-layer code** are *post-parse*: argv parsing succeeded, the
command function entered, and `--json`'s value (if any) is bound on the
Click context. These failures route through `output_error(...)` (the
canonical envelope emitter) and exit `1` with the typed JSON error
envelope under `--json` or a plain stderr message in text mode. The
typical code is `VALIDATION_ERROR`. New command/service code MUST NOT
raise `ClickException` for post-parse validation failures unless the call site
is an intentional CLI input-validation boundary marked inline with
`# cli-input-validation: <reason>`. Raw `SystemExit` sites outside the central
handler are similarly marked with `# cli-raw-exit: <reason>`. The guardrail is
[`tests/_guardrails/test_error_handler_allowlist.py`](../tests/_guardrails/test_error_handler_allowlist.py);
see [ADR-0015](adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md)
for the contract and rationale.

## JSON output mode (`--json`)

When a command supports `--json` and the flag is set,
errors are emitted as a JSON document on stdout *and* the exit code still
applies. The shape is:

```json
{
  "error": true,
  "code": "RATE_LIMITED",
  "message": "Error: Rate limited. Retry after 30s.",
  "retry_after": 30
}
```

The `code` field is the stable identifier (see table above); `message` is the
human string and may change. Some errors include extra fields
(`retry_after`, `method_id` when `-v/--verbose` is set, etc.). Automation
should branch on `code` (or, more simply, on the exit code).

**Post-parse `ClickException` is covered.** Validation failures that a
command body or its service-layer code chooses to express by raising
`click.UsageError` / `click.BadParameter` / `click.ClickException` (for
example, a flag-combination conflict detected after argv parsing
succeeds) are routed through this same envelope under `--json` and exit
`1` with `code: "VALIDATION_ERROR"` (or another standard code chosen by
the raise site). The contract decision and its enumerated raise sites
are recorded in
[ADR-0015](adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md).

**Parse-time `ClickException` is also covered under `--json`.**
`ClickException` raised by Click's own parser (before the command body runs)
is JSON-wrapped at the root group (`SectionedGroup.main`), not by the
per-command `handle_errors(...)`: under `--json` it emits the same envelope on
stdout with `code: "VALIDATION_ERROR"` and an **exit code preserved** from
Click (`2` for `UsageError` / `BadParameter`, `1` for the base
`ClickException`). In text mode it is unchanged — Click renders its
`Usage: ... / Error: ...` text on stderr and exits with that class-default
code. This is the more consistent behavior for JSON consumers: argv-level
failures no longer fall back to usage prose just because they fired before the
command body. The behavior is pinned by
[`tests/unit/cli/test_json_validation_contract.py`](../tests/unit/cli/test_json_validation_contract.py)
(`test_json_validation_errors_emit_json`, `test_text_validation_errors_keep_click_usage_output`)
and amended into ADR-0015 (amendment note, 2026-06-02).

## Intentional exceptions to the standard convention

Two commands deliberately extend the standard codes (or expose an opt-in
inversion) because their primary use case is shell control flow. Code
referencing them should comment the inverted/extended semantics. Both
exceptions are stable contracts: `source wait`'s three-way exit (`0`/`1`/`2`)
is by design and will not change; `source stale` follows the standard
convention by default and only inverts when callers explicitly pass
`--exit-on-stale`.

<a id="source-stale-exit-on-stale"></a>

### `notebooklm source stale <SOURCE_ID>` — opt-in inverted predicate

Implemented by `source_stale` + `_render_source_stale_result` in
[`src/notebooklm/cli/source_cmd.py`](../src/notebooklm/cli/source_cmd.py).
Default behavior was previously the inverted predicate (`0=stale, 1=fresh`)
but has been standardised; the inversion is now an explicit opt-in via
`--exit-on-stale`.

Default (no flag) — standard CLI convention:

| Exit | Meaning |
|------|---------|
| `0`  | Freshness check succeeded (source may be **stale** or **fresh** — branch on stdout text or, with `--json`, on the `stale`/`fresh` fields) |
| `1`  | Error (auth, network, validation, unresolvable source ID, etc. — raised by `handle_errors`) |

Opt-in with `--exit-on-stale` — back-compat inverted predicate:

| Exit | Meaning |
|------|---------|
| `0`  | Source is **stale** (needs `source refresh`) |
| `1`  | Source is **fresh** **or** an error occurred (ambiguous — see below) |

The inversion preserves the natural shell idiom for callers that depend on it:

```bash
if notebooklm source stale --exit-on-stale "$SRC_ID"; then
    notebooklm source refresh "$SRC_ID"
fi
```

A `0` exit (with `--exit-on-stale`) reads as "yes, the predicate (stale)
holds, run the body" — the same convention as `test`, `grep -q`, etc.

> **Important — exit-1 ambiguity (only with `--exit-on-stale`).** The
> command is wrapped by the standard `handle_errors` context, so
> `AuthError`, `NetworkError`, `ValidationError`, an unresolvable source
> ID, etc. *also* exit `1` under `--exit-on-stale` and are indistinguishable
> from "source is fresh" by exit code alone. The naive `if`-chain above
> will silently skip the refresh body on an auth/network outage. For
> unattended scripts, validate the session first (`notebooklm auth check --test`
> for a live RPC probe, or `notebooklm auth check` for local cookie checks),
> wrap with `|| die "..."` on the predicate, or
> branch on the JSON `stale`/`fresh` fields with the default (non-opt-in)
> semantics where success and freshness verdict are decoupled.

Note: under `set -e` the `1` exit (when fresh, with `--exit-on-stale`)
will abort the script. Use the predicate inside an `if`/`elif`/`||` (as
above), which shell's errexit explicitly excludes, or `set +e` around the
call. The default semantics (no flag) do not have this hazard — the
command exits `0` on success regardless of freshness.

<a id="source-wait-three-way"></a>

### `notebooklm source wait <SOURCE_ID>` — three-way

Implemented by `source_wait` in
[`src/notebooklm/cli/source_cmd.py`](../src/notebooklm/cli/source_cmd.py) (the
exit-code table is in the command's docstring, around lines 1080-1084 at
the time of writing).

| Exit | Meaning |
|------|---------|
| `0`  | Source is ready |
| `1`  | Source not found or processing failed |
| `2`  | Timeout reached before the source became ready |

This is the only command whose `2` exit does **not** indicate a bug — it is
a recoverable condition the caller may want to retry with a longer
`--timeout`. Scripts that distinguish "transient" from "fatal" should branch
on the specific code rather than the truthy/falsy value:

```bash
notebooklm source wait "$SRC_ID" --timeout 300
case $? in
  0)  echo "ready" ;;
  1)  echo "failed"; exit 1 ;;
  2)  echo "timed out, retry later"; exit 75 ;;  # EX_TEMPFAIL
  *)  echo "unexpected"; exit 1 ;;
esac
```

## Recipes for callers

### Shell

```bash
# Standard — non-zero is failure
notebooklm ask -n "$NOTEBOOK_ID" "Summarize"
status=$?
if [ "$status" -ne 0 ]; then
    echo "ask failed (exit $status)" >&2
    exit 1
fi

# Distinguish bug from user error
notebooklm <cmd> --json > out.json
case $? in
  0)   ;;                                 # success
  1)   jq -r .code out.json ;;            # user/app error — branch on code
  2)   echo "internal CLI error" >&2 ;;   # bug; report it
  130) echo "cancelled by user" >&2 ;;    # ^C
esac
```

### Python `subprocess`

```python
import json
import subprocess
import time

result = subprocess.run(
    ["notebooklm", "ask", "-n", nb_id, prompt, "--json"],
    capture_output=True, text=True,
)
if result.returncode == 0:
    payload = json.loads(result.stdout)
elif result.returncode == 1:
    err = json.loads(result.stdout)  # JSON error document
    if err["code"] == "RATE_LIMITED":
        time.sleep(err.get("retry_after", 30))
elif result.returncode == 2:
    raise RuntimeError(f"CLI bug: {result.stdout}")
elif result.returncode == 130:
    raise KeyboardInterrupt
```

## Migration notes

The following shifts have landed (or are about to land) as part of the CLI
UX overhaul and are documented here for callers preparing for — or recovering
from — the contract change.

<a id="get-on-not-found-exits-1-was-0-landed"></a>

### `get`-on-not-found exits `1` (was `0`) ✅ **Landed**

`notebooklm source get`, `notebooklm artifact get`, and `notebooklm note get`
**now exit `1`** with the typed JSON error envelope (`{error, code:
"NOT_FOUND", message, ...}` under `--json`; plain "X not found" on stderr
otherwise) when the requested ID is missing. Previously they printed a "not
found" message to stdout and exited `0`. The new contract matches the rest of
the CLI's user-error convention and lets scripts branch on the exit code
without parsing output text:

```bash
# Idiomatic
if ! notebooklm source get "$SRC_ID"; then
    handle_missing "$SRC_ID"
fi

# JSON form — branch on the typed code
notebooklm source get "$SRC_ID" --json > out.json
case $? in
  0) ;;                              # found; ``out.json`` mirrors the Source dataclass
  1) jq -r .code out.json ;;         # ``NOT_FOUND`` here, but auth/network errors also land
esac
```

This **breaks** any shell script that relied on exit-`0`-on-not-found (e.g.
`notebooklm source get X | grep -q '<title>' && do_something`). Such scripts
must switch to the new exit-code branch shown above. The message text is also
no longer printed to stdout (it's on stderr now), so `grep`-on-stdout for
"not found" likewise stops working — branch on the exit code instead.

The change covers **both** code paths: input IDs ≥20 chars (which skip the
partial-resolve list round-trip in `_resolve_partial_id`) and the rare
race where partial-resolve succeeds but the subsequent `get` returns
`None` because the row was deleted between the two calls.

The pre-existing "no partial-ID match" branch (raised by `_resolve_partial_id`
as a `ClickException`) was already exit `1` and is unchanged.

### `note`/`artifact delete --json` without `--yes`, `note rename` race exit `1` (was `0`) ✅ **Landed**

These surgical fixes match the broader `--json` exit-code convention pinned
by the prior `get`-on-not-found change. The affected cases previously emitted
a `{"<verb>ed": false, "error": ...}` payload on exit `0`, which passed
silently in `set -e` / `check_call`-style scripts branching on the exit code.

`notebooklm note delete <id> --json` without `--yes` cannot prompt (it would
corrupt the parseable-JSON contract callers depend on), but it must also not
appear to succeed. The command now emits the standard typed envelope:

```json
{
  "error": true,
  "code": "VALIDATION_ERROR",
  "message": "Pass --yes to confirm deletion in --json mode",
  "id": "...",
  "notebook_id": "..."
}
```

on stdout and exits `1`. The text-mode interactive prompt path (no `--json`)
is unchanged — declining the prompt is still a no-op exit `0`.

`notebooklm artifact delete <id> --json` follows the same destructive-operation
guard: without `--yes`, it now emits `VALIDATION_ERROR`, includes the resolved
artifact and notebook IDs plus `"deleted": false`, exits `1`, and does not
delete the artifact. Passing `--yes` preserves the existing successful JSON
payload (`{"id": "...", "deleted": true}`).

`notebooklm note rename <id> "new title"` resolves the partial ID and then
fetches the current note to preserve its content before issuing the update.
A concurrent `note delete` can win the race between those two calls; the
backend then returns `None` and the rename has nothing to update. The
command now funnels that race into the same typed `NOT_FOUND` envelope used
by `note get`'s Path B:

```json
{
  "error": true,
  "code": "NOT_FOUND",
  "message": "Note not found",
  "id": "...",
  "notebook_id": "..."
}
```

on stdout (text mode: plain `Note not found` on stderr) and exits `1`. The
update RPC is **never** issued on this path — callers can rely on the
absence of side effects when they see this envelope.

**Migration:** scripts branching on the exit code now correctly catch both
misconfigurations. Scripts parsing the JSON body must switch from
`data["deleted"] == false` / `data["renamed"] == false` to
`data["error"] == true` (or branch on `data["code"]`).

### `download` exception paths route through the typed handler

The `download` command group routes all `download` exception paths through `handle_errors` (`cli/download_cmd.py`) so that:

- `--json` consistently produces the JSON error document on every failure.
- Exit codes match the standard table above (`1` for known library errors,
  `2` for unexpected, `130` for `^C`).

## See also

- [CLI Reference](cli-reference.md) — command-by-command documentation
- [Configuration](configuration.md) — `--json` and global options
- [Troubleshooting](troubleshooting.md) — interpreting common errors
- [`src/notebooklm/cli/error_handler.py`](../src/notebooklm/cli/error_handler.py)
  — canonical implementation

## Exit code semantics

This is the normative one-line summary of the convention every
`notebooklm` CLI command obeys unless it appears in the
[Intentional exceptions](#intentional-exceptions-to-the-standard-convention)
section above.

| Code | Semantic meaning |
|------|------------------|
| `0`  | The command succeeded as documented — the requested effect was carried out and any reported result is authoritative. |
| `1`  | The command failed, **or** the queried target was not found. Both share exit `1` because automation typically wants the same control-flow branch (`if !` / `case 1)`); JSON mode (`--json`) distinguishes them via the typed `code` field (`NOT_FOUND` vs. `AUTH_ERROR` vs. `VALIDATION_ERROR`, etc.). |
| `2`  | Click parser-time error — argv could not be parsed into a valid command invocation (unknown flag, type-validation failure, missing required argument). Under `--json` the root group still emits the typed JSON envelope on stdout (`code: "VALIDATION_ERROR"`) but **preserves** this exit `2`; in text mode Click renders its `Usage:/Error:` prose on stderr. See the [parser-time row in the Exception → exit-code mapping](#exception-exit-code-mapping) for the full behavior; this entry exists to call out that `2` is **not** a post-parse code in the default case. Post-parse `ClickException` is contracted by [ADR-0015](adr/0015-json-envelope-contract-for-post-parse-click-exceptions.md) to route through the typed JSON envelope and exit `1`, not `2`. The same code is also raised when `handle_errors` catches an unhandled non-`NotebookLMError` exception (likely a bug — see the [Standard exit codes](#standard-exit-codes) table). |

Two commands deliberately deviate from this baseline because their primary
use case is shell control flow:

- `source wait` extends the table with `2` = timeout (a recoverable condition,
  not a bug — the only command where `2` is not a parser-time error). See
  [`notebooklm source wait`](#source-wait-three-way).
- `source stale` offers an opt-in inverted predicate via `--exit-on-stale`
  (`0=stale, 1=fresh`) for back-compat with the `if … ; then refresh; fi`
  idiom. The default now follows the standard convention. See
  [`notebooklm source stale`](#source-stale-exit-on-stale).

`130` (Ctrl-C / SIGINT) is signal-driven and orthogonal to the
success/failure axis; it is documented in the
[Standard exit codes](#standard-exit-codes) table above.
