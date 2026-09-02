# CLI exit-code convention

**Status:** Active
**Last Updated:** 2026-09-02

Use the process exit status for shell control flow. In `--json` mode, use the
stable JSON `code` to distinguish error categories; human-readable messages
may change.

<a id="exit-code-semantics"></a>

## Standard codes

| Code | Meaning |
|---:|---|
| `0` | The command succeeded. |
| `1` | A known command, input, authentication, network, rate-limit, configuration, or not-found error occurred. |
| `2` | An unexpected internal error, or an argv parsing error reported by Click. |
| `130` | The user interrupted the command (Ctrl-C / SIGINT). |

Known library errors exit `1`. In JSON output, common codes include
`AUTH_ERROR`, `CONFIG_ERROR`, `NETWORK_ERROR`, `NOT_FOUND`, `RATE_LIMITED`,
and `VALIDATION_ERROR`. An unhandled exception is `UNEXPECTED_ERROR` and exits
`2`.

The complete central mapping is:

| Exception or failure | JSON `code` | Exit |
|---|---|---:|
| Any emitted exception marked `unconfirmed=True` | `UNCONFIRMED_WRITE` | Inherits the matched branch (`1` for handled library errors; `2` for an unexpected exception) |
| `RateLimitError` | `RATE_LIMITED` | `1` |
| `AuthError` | `AUTH_ERROR` | `1` |
| `ValidationError` | `VALIDATION_ERROR` | `1` |
| `ConfigurationError` | `CONFIG_ERROR` | `1` |
| `NetworkError` | `NETWORK_ERROR` | `1` |
| `NotebookLimitError` | `NOTEBOOK_LIMIT` | `1` |
| `ArtifactTimeoutError` | `ARTIFACT_TIMEOUT` | `1` |
| `NotFoundError` and domain `*NotFoundError` | `NOT_FOUND` | `1` |
| Other `NotebookLMError` | `NOTEBOOKLM_ERROR` | `1` |
| `KeyboardInterrupt` | `CANCELLED` | `130` |
| Unhandled `Exception` | `UNEXPECTED_ERROR` | `2` |

The `UNCONFIRMED_WRITE` override takes precedence over the exception-type mapping. It means the
write may have committed, so callers should reconcile state before retrying to avoid duplicates.

Parse-time Click usage/parameter failures use `VALIDATION_ERROR` under `--json` while preserving
Click's exit (`2` for usage errors, otherwise the Click exception's exit). Post-parse Click
validation raised from a command body uses `VALIDATION_ERROR` and exits `1`.

Click parser errors (for example an unknown flag or a missing required value)
normally exit `2`. With `--json`, the root command emits a
`VALIDATION_ERROR` envelope on stdout while preserving Click's exit status;
without `--json`, Click writes its usage/error text to stderr.

## JSON errors

Commands that support `--json` emit error JSON on stdout and still return a
non-zero status:

```json
{
  "error": true,
  "code": "RATE_LIMITED",
  "message": "Error: Rate limited. Retry after 30s.",
  "retry_after": 30
}
```

Treat `code` as the machine-readable category. Extra fields are command- and
error-specific; for example, `NOT_FOUND` responses can include an `id` and a
resource-specific ID field.

```bash
notebooklm ask -n "$NOTEBOOK_ID" "Summarize" --json >out.json
case $? in
  0) ;;                                      # success
  1) jq -r '.code' out.json >&2 ;;           # expected command failure
  2) echo "invalid invocation or CLI bug" >&2 ;;
  130) echo "cancelled" >&2 ;;
esac
```

## Command-specific contracts

<a id="source-stale-exit-on-stale"></a>

### `source stale`

By default, `notebooklm source stale ID` exits `0` whenever the freshness
check itself succeeds, whether the source is stale or fresh. In JSON output,
branch on the `stale` field.

`--exit-on-stale` enables the legacy predicate form: `0` means stale and `1`
means fresh. With that option, `1` is also used for ordinary errors, so it is
not safe to treat every `1` as “fresh” in unattended automation. Prefer the
default mode plus JSON when that distinction matters.

### `source wait`

`notebooklm source wait ID` has a deliberate three-way contract:

| Code | Meaning |
|---:|---|
| `0` | Source is ready. |
| `1` | Source was not found or processing failed. |
| `2` | Timeout elapsed before the source became ready. |

Here `2` is a recoverable timeout, not an internal failure.

<a id="get-on-not-found-exits-1-was-0-landed"></a>

## Not-found migration

`source get`, `artifact get`, and `note get` exit `1` for a missing resource.
Under `--json` they emit `code: "NOT_FOUND"`; otherwise the message is written
to stderr. Scripts that relied on the old successful exit status must instead
handle exit `1` and, when needed, inspect the JSON code.

## See also

- [CLI reference](cli-reference.md)
- [Configuration](configuration.md)
- [Troubleshooting](troubleshooting.md)
