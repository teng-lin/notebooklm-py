# CLI Exit-Code Convention

**Status:** Active
**Last Updated:** 2026-05-14

This document defines the exit-code policy for the `notebooklm` CLI. Shell
scripts, CI pipelines, and AI-agent automations should rely on these codes for
control flow rather than scraping stdout/stderr text — the text is intended for
humans and may evolve, but the exit-code contract is stable.

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

The policy comment in `error_handler.py:64-67` is the source of truth:

```text
Exit codes:
    1: User/application error (validation, auth, rate limit, etc.)
    2: System/unexpected error (bugs, unhandled exceptions)
    130: Keyboard interrupt (128 + signal 2)
```

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
| `click.UsageError` / `click.BadParameter` (bad CLI args) | — | re-raised; Click exits `2` |
| Other `click.ClickException` subclasses                  | — | re-raised; Click exits `1` |

`click.ClickException` and its subclasses are intentionally re-raised so
Click can render its own `Usage: ...` / `Error: ...` message. The exit code
is whatever Click's own `exit_code` class attribute provides — `2` for
`UsageError` (and `BadParameter`, which subclasses it), `1` for the base
`ClickException` and other non-usage subclasses. This aligns Click's "bad
arguments" exit (`2`) with our "system/unexpected" code, and Click's other
exceptions with our "user/app error" code, so callers can branch on the
exit code without distinguishing the two sources.

## JSON output mode (`--json`)

When a command supports `--json` (or `--json-output`) and the flag is set,
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

## Intentional exceptions to the standard convention

Two commands deliberately invert or extend the standard codes because their
primary use case is shell control flow. **These are by design and will not
change.** Code referencing them should comment the inverted semantics.

### `notebooklm source stale <SOURCE_ID>` — inverted

Implemented by `source_stale` in
[`src/notebooklm/cli/source.py`](../src/notebooklm/cli/source.py) (around
line 1146 at the time of writing).

| Exit | Meaning |
|------|---------|
| `0`  | Source is **stale** (needs `source refresh`) |
| `1`  | Source is **fresh** (no action required) |

The inversion lets you write the natural shell idiom:

```bash
if notebooklm source stale "$SRC_ID"; then
    notebooklm source refresh "$SRC_ID"
fi
```

A `0` exit reads as "yes, the predicate (stale) holds, run the body" — the
same convention as `test`, `grep -q`, etc.

> **Important — exit-1 ambiguity.** `source stale` is wrapped by the
> standard `handle_errors` context, so `AuthError`, `NetworkError`,
> `ValidationError`, an unresolvable source ID, etc. *also* exit `1` and are
> indistinguishable from "source is fresh" by exit code alone. The naive
> `if`-chain above will silently skip the refresh body on an auth/network
> outage. For unattended scripts, validate the session first
> (`notebooklm status` or `notebooklm auth check`), wrap with `|| die "..."`
> on the predicate, or check `source get` succeeds before relying on the
> staleness verdict.

Note: under `set -e` the `1` exit when the source is fresh will abort the
script. Use the predicate inside an `if`/`elif`/`||` (as above), which
shell's errexit explicitly excludes, or `set +e` around the call.

### `notebooklm source wait <SOURCE_ID>` — three-way

Implemented by `source_wait` in
[`src/notebooklm/cli/source.py`](../src/notebooklm/cli/source.py) (the
exit-code table is in the command's docstring, around lines 1113-1116 at
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
if ! notebooklm ask "$NOTEBOOK_ID" "Summarize"; then
    echo "ask failed (exit $?)" >&2
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
    ["notebooklm", "ask", nb_id, prompt, "--json"],
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

### `note delete --json` without `--yes`, `note rename` race exit `1` (was `0`) ✅ **Landed**

Two parallel surgical fixes to `cli/note.py` matching the broader `--json`
exit-code convention pinned by the prior `get`-on-not-found change. Both
cases previously emitted a `{"<verb>ed": false, "error": ...}` payload on
exit `0`, which passed silently in `set -e` / `check_call`-style scripts
branching on the exit code.

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

The `download` command group routes all `download` exception paths through `handle_errors` (`cli/download.py:699-737`) so that:

- `--json` consistently produces the JSON error document on every failure.
- Exit codes match the standard table above (`1` for known library errors,
  `2` for unexpected, `130` for `^C`).

## See also

- [CLI Reference](cli-reference.md) — command-by-command documentation
- [Configuration](configuration.md) — `--json` and global options
- [Troubleshooting](troubleshooting.md) — interpreting common errors
- [`src/notebooklm/cli/error_handler.py`](../src/notebooklm/cli/error_handler.py)
  — canonical implementation
