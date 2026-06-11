# ADR-0015: Typed JSON error envelope covers post-parse `ClickException` failures

## Status

Accepted. **Amended 2026-06-02** — see the amendment note below; it supersedes
the original "no JSON emitted at parse time" stance recorded in Decision rule 1.

## Amendment note (2026-06-02): parse-time `ClickException` is JSON-wrapped under `--json`

The original Decision rule 1 stated that parse-time `ClickException` is
*unchanged* and that "no JSON envelope is emitted in this case, because
`handle_errors` has not yet been entered when the parser fires." That premise
was correct about `handle_errors` — the per-command context manager genuinely
never sees parse-time errors — but the conclusion no longer holds, because the
CLI grew a **second, higher** boundary above `handle_errors`.

The root group (`SectionedGroup.main` in
[`src/notebooklm/cli/grouped.py`](../../src/notebooklm/cli/grouped.py)) runs
Click's superclass `main` in **non-standalone mode** specifically so it can
catch the parse-time parser/callback `click.ClickException` that Click would
otherwise render itself. Its docstring records the intent: catching the failure
at "this root boundary [lets it] convert those failures once for every current
and future subcommand option." When the raw argv contains `--json`
(`_json_requested(args)`), the root group routes the exception through
`_emit_json_click_error(exc)`, which calls `output_error(exc.format_message(),
"VALIDATION_ERROR", json_output=True, exit_code=exc.exit_code)`. Concretely,
under `--json`:

- The typed JSON envelope is emitted on stdout — `{ "error": true, "code":
  "VALIDATION_ERROR", "message": "<Click's formatted message>" }` — and nothing
  is written to stderr.
- **Exit codes are preserved**, not normalised. The envelope passes
  `exit_code=exc.exit_code`, so a `UsageError` / `BadParameter` still exits `2`
  and a base `ClickException` still exits `1`. (This differs from the
  *post-parse* envelope path, which standardises on exit `1` per Decision rules
  2 and 4. The parse-time wrapper changes the *channel* — JSON instead of
  stderr — but not the exit code.)

Without `--json`, behavior is exactly as the original ADR described: the root
group calls `exc.show()` (Click's native `Usage: ... / Error: ...` text on
stderr) and exits with `exc.exit_code` (`2` for `UsageError` / `BadParameter`,
`1` for the base `ClickException`). The `click.Abort` path is handled the same
way at this boundary — `CANCELLED` envelope + exit `1` under `--json`, or
`Aborted!` on stderr + exit `1` in text mode.

**Why the change.** The motivation is the same one that drove the original
contract for post-parse errors: a JSON consumer that passes `--json` should
never receive usage prose on stderr instead of a parseable envelope, even when
the failure is an argv-level one (an invalid `--limit`, an unknown option, a
missing required argument). Converting once at the root boundary gives every
current and future subcommand a consistent envelope without per-command
plumbing, and is strictly more useful to automation than the original
"argv-level failures are structurally impossible to wrap" stance — which was
true *inside Click's parser* but not at a boundary that runs the parser in
non-standalone mode and catches what it raises.

**Source of truth.** This behavior is pinned by
[`tests/unit/cli/test_json_validation_contract.py`](../../tests/unit/cli/test_json_validation_contract.py):
`test_json_validation_errors_emit_json` asserts the `VALIDATION_ERROR` envelope
on stdout (empty stderr, non-zero exit) for invalid limits/intervals/retries, a
missing argument, an unknown option, and a root-callback validation failure;
`test_text_validation_errors_keep_click_usage_output` asserts the unchanged
text-mode path (exit `2`, `Usage:` on stderr, empty stdout); and
`test_json_abort_emit_json` pins the `CANCELLED` envelope. The original
Decision rule 1 below is retained for history; treat this note and the contract
tests as authoritative wherever they diverge.

## Context

The CLI ships a stable error contract for automation: under `--json`, every
fatal command path emits a *typed JSON error envelope*
on stdout — a flat object of the shape

```json
{ "error": true, "code": "<STABLE_CODE>", "message": "<human text>", ...extras }
```

— and the process exits with the corresponding code from the table in
[`docs/cli-exit-codes.md`](../cli-exit-codes.md). The canonical implementation
is `_output_error(...)` in
[`src/notebooklm/cli/error_handler.py`](../../src/notebooklm/cli/error_handler.py).

That contract was clear for library exceptions (`AuthError`, `RateLimitError`,
`ValidationError`, ...). It was **not** clear for `click.ClickException` and
its subclasses (`click.UsageError`, `click.BadParameter`, and bare
`ClickException`), because `handle_errors(...)` deliberately re-raises them
unmodified in its `except click.ClickException: raise` clause in
[`src/notebooklm/cli/error_handler.py`](../../src/notebooklm/cli/error_handler.py).
Click then renders its own `Usage: ... / Error: ...` prose to stderr and exits
with its class-level `exit_code` (`2` for `UsageError`/`BadParameter`, `1` for
the base `ClickException`).

There are two different phases in which `ClickException` subclasses are
raised, and they behave identically at the Click level but mean very
different things to a caller:

- **Parse-time** raises happen *before* the command body runs. Click's own
  parser raises `UsageError`/`BadParameter` when argv fails option/type
  validation (e.g. `--limit foo` where `--limit` is `IntRange`, an unknown
  flag, a missing required argument). The command function has not been
  invoked; `handle_errors(...)` is not yet on the stack; `--json` may have
  been *typed* on the command line but the parser never finished resolving
  it, so the caller's expectation of a JSON envelope is structurally
  impossible to honour from inside Click's parser.
- **Post-parse** raises happen *inside* the command body or the service
  layer it calls, after argv parsing succeeded and after the `--json` flag's
  value is bound in `click.Context.params`. These are validation decisions
  the program itself made — flag-combination conflicts, computed
  preconditions, file-format checks — that happen to be expressed by
  raising a `ClickException` subclass instead of one of the library
  exception types. They reach the `except click.ClickException: raise`
  branch in `error_handler.py` and skip the envelope.

The CLI audit (the **P1#2
"command-body `UsageError`/`BadParameter` bypass"** finding at lines 54-90)
enumerated post-parse raise sites that ride this bypass:

- `src/notebooklm/cli/services/download.py:257` — `--force` / `--no-clobber`
  conflict
- `src/notebooklm/cli/services/generate.py:394` — `--style custom` requires
  `--style-prompt`
- `src/notebooklm/cli/research_cmd.py:158` — `--cited-only` requires
  `--import-all`
- `src/notebooklm/cli/source_cmd.py:626` — `--cited-only` requires
  `--import-all`
- `src/notebooklm/cli/chat_cmd.py:193` — `--new` mutually exclusive with
  `--conversation-id`
- `src/notebooklm/cli/generate_cmd.py:57` — language-code validation

For each of these, `<cmd> ... --json` exits `2` with Click's usage text on
stderr and **no JSON on stdout**. Automation branching on
`json.loads(stdout)` (the published recipe in `docs/cli-exit-codes.md`)
breaks. The meta-audit
reframed the finding in three ways that matter here:

- **C1** declared the contract decision a stop-sentinel: no implementation
  PR should reshape these sites until the contract is decided and recorded,
  because two of the candidate fix shapes (route through the envelope
  vs. waive `ClickException` from `--json` entirely) drive contradictory
  patches.
- **C3** widened the layer attribution: the same shape exists in service
  modules (`cli/services/generate.py:383, 394, 396, 398`,
  `cli/services/download.py:257, 259, 261`,
  `cli/services/source_mutations.py:18`), so any contract decision applies
  to both command and service code.
- **I1** corrected the audit's claim that `docs/cli-exit-codes.md` was
  "internally ambiguous". The doc was **silent** on post-parse
  `UsageError`: line 52's table row describes Click re-raising its own
  exceptions, line 64's JSON section describes envelopes for library
  exceptions enumerated above, and the doc never crosses the two. This ADR
  fills that gap.

## Decision

Under `--json`, **every post-parse
`click.ClickException`-subclass failure raised from a command body or from
the service layer it calls emits the typed JSON error envelope** defined in
[`docs/cli-exit-codes.md`](../cli-exit-codes.md), exits with the
corresponding standard code, and writes no usage text to stderr.

Concretely:

1. **Parse-time `ClickException` is unchanged.** *(Superseded — see the
   "Amendment note (2026-06-02)" above. The root group now JSON-wraps
   parse-time `ClickException` under `--json`, preserving Click's exit code.
   The text below is retained for history and remains accurate only for the
   non-`--json` path.)* Click's parser keeps raising
   `UsageError` / `BadParameter` / `ClickException` for argv-level
   validation failures; Click keeps rendering its own
   `Usage: ... / Error: ...` text on stderr and exiting `2`
   (`UsageError` / `BadParameter`) or `1` (base `ClickException`). The
   re-raise in
   [`src/notebooklm/cli/error_handler.py`](../../src/notebooklm/cli/error_handler.py)
   stays. No JSON envelope is emitted *by `handle_errors`* in this case,
   because `handle_errors` has not yet been entered when the parser fires —
   but the root group (`SectionedGroup.main`) sits *above* `handle_errors`
   and does emit the envelope under `--json`; see the amendment note.
2. **Post-parse `ClickException` flows through the typed envelope.**
   Command bodies and service-layer code that wish to signal a validation
   failure under the JSON contract MUST NOT raise
   `click.UsageError` / `click.BadParameter` / `click.ClickException`
   directly; they must instead call `output_error(...)` from
   `cli.error_handler` (or raise a library `ValidationError` /
   `ConfigurationError` that `handle_errors(...)` already maps onto the
   envelope). The natural code for a flag-combination or precondition
   conflict is `VALIDATION_ERROR` (exit `1`); the existing exit-code table
   in `docs/cli-exit-codes.md` lists the full mapping. No new error-code
   keys are introduced by this ADR.
3. **The wire shape is unchanged.** Envelopes remain the flat object
   `{ "error": true, "code": "<CODE>", "message": "<text>", ...extras }`
   already produced by `_output_error`. Callers that already parse
   `json.loads(stdout)` and branch on `code` need no migration.
4. **The text-mode contract is preserved.** When `--json` is *not* set,
   post-parse validation failures still surface a human-readable message
   on stderr and exit with the corresponding standard code. For sites
   refactored under rule 2, the message comes from `output_error(...)`
   rather than Click's `Usage: ... / Error: ...` formatter, so it omits
   the command-usage footer; this is the smallest user-visible delta
   consistent with rule 2. Marked sites (rule 5) keep Click's
   formatter unchanged.
5. **Marked residual `ClickException` raises.** A small number of
   sites are correctly raising `ClickException` subclasses today and
   should keep doing so — they sit on input-validation boundaries before
   the command produces any output, and matching Click's own
   parser-style error rendering is the right UX (e.g. UTF-8 / file-read
   validation in `cli/input.py`, profile-name argument validation in
   `cli/profile_cmd.py`, entity-ID argument validation in `cli/resolve.py`,
   shared profile-name validation in `cli/services/login/profile_targets.py`).
   These are tracked exhaustively by inline marker comments:
   `# cli-input-validation: <reason>` for envelope-bypassing Click exceptions
   and `# cli-raw-exit: <reason>` for raw `SystemExit` sites outside
   `error_handler.py`. `tests/_guardrails/test_error_handler_allowlist.py`
   enforces that markers are present, non-empty, and not stale. New sites
   require a local marker with a justification; the default for any *other*
   post-parse validation failure is rule 2.

## Consequences

**Wanted:**

- `--json` is a reliable machine contract for every command-body failure.
  Automation branching on `json.loads(stdout)` and `code` works for
  validation conflicts the same way it already works for `RATE_LIMITED`
  and `AUTH_ERROR`.
- `docs/cli-exit-codes.md` is no longer silent on post-parse `UsageError`,
  which closes meta-audit item I1 and removes a class of "but the docs
  said..." reports.
- Command and service code converges on one error-emission path
  (`output_error(...)` / library exceptions through `handle_errors(...)`),
  which composes cleanly with the `cli/services/` extraction pattern in
  [ADR-0008](./0008-cli-services-extraction-pattern.md). Service modules no
  longer need to choose between raising Click exceptions (skips the
  envelope) and importing `output_error` (couples to the CLI layer); the
  ADR-0008 boundary becomes the right place to enforce typed-outcome
  returns. Service-layer convergence work referenced under meta-audit C3
  lands as separate per-site PRs that cite this ADR.
- Tests can assert the envelope shape with the existing JSON-purity sweeps
  (`tests/unit/test_json_error_exit.py`, `tests/unit/test_json_stdout_purity.py`)
  by adding error-path argv cases for the previously-bypassed sites; no
  new test infrastructure is required.

**Unwanted:**

- Some post-parse failures previously rendered with Click's
  `Usage: ... / Error: ...` footer. Under rule 4, text-mode output for
  those sites loses the usage footer in exchange for a consistent stderr
  message. The audit considers this an acceptable trade because the
  usage-footer convention is for argv-shape errors, and post-parse
  failures are semantic-shape errors that already explain themselves in
  the message body.
- Exit codes for post-parse failures shift from `2` (Click's
  `UsageError.exit_code`) to `1` (the standard `VALIDATION_ERROR` exit).
  This is intentional: `2` is reserved for system/unexpected errors and
  the parse-time path; a flag-combination failure is a user/application
  error and belongs at `1`. Scripts that branched on `?==2` after a
  post-parse `UsageError` would have been confusing "bad argv" with "bad
  combination" anyway; the new code is unambiguous.
- The ADR creates a permanent split between parse-time and post-parse
  `ClickException` handling. Reviewers must apply rule 2 when adding new
  validation in command/service code, and reviewers must apply rule 1
  when changing Click parser configuration. The split is documented here
  so the next reviewer does not re-litigate it.
- Pattern A and Pattern B service-layer sites enumerated by meta-audit C4
  (~10 modules) still need follow-up PRs to actually route through the
  envelope; this ADR records the contract but does not in itself move any
  code. Each follow-up PR cites this ADR and removes one site from the
  bypass.

## Alternatives considered

- **Waive `ClickException` subclasses from `--json` entirely; document the
  exception.** Rejected. This freezes the audit-flagged behaviour as
  contractual: callers using `--json` for automation still cannot rely on
  JSON output for command-body validation failures, and the doc would
  have to enumerate which validations are "covered" and which are not.
  The matrix is unstable as new commands are added, and the audit's P1
  ranking specifically calls out automation breakage as the
  highest-impact user-facing issue (meta-audit C2).
- **Convert every `ClickException` raise to a library `ValidationError`.**
  Rejected as a contract decision (but accepted as one valid *fix shape*
  per site). Some current `ClickException` sites are genuinely argv-level
  (the allowlisted ones in rule 5 of the Decision); forcing them through
  `ValidationError` would surface less-helpful messages to interactive
  users. The decision is "route through the envelope", not "stop using
  Click exceptions"; the allowlist exists precisely so each site can
  choose the right shape.
- **Plumb `json_output` into every service helper and have each call
  `output_error(..., json_output=...)` directly from the helper.**
  Rejected as the *primary* contract shape (it would couple service
  modules to the CLI error layer, contradicting ADR-0008's boundary), but
  accepted as the smallest viable patch for any single site under
  remediation pressure. The preferred long-term shape is "service returns
  a typed outcome; command routes outcome through `output_error`"; the
  short-term patch is "service calls `output_error` directly". Both
  satisfy this ADR.
- **Have `handle_errors` catch `ClickException` and convert to the
  envelope unconditionally.** Rejected *for `handle_errors`*. This would
  absorb parse-time `ClickException` too — but `handle_errors` is entered
  *inside* the command body, so the parse-time path never reaches it (Click
  renders parser errors via `BaseCommand.main` before invoking the command).
  Intercepting parse-time errors requires subclassing Click's command/group
  machinery, which was originally judged outside the scope of an
  error-handling contract change. The post-parse path is the only one
  `handle_errors` can address, and rule 2 already covers it via
  `output_error(...)`.

  *(Update 2026-06-02: the "subclass Click's group machinery" approach was
  subsequently adopted — but at the root group, not in `handle_errors`.
  `SectionedGroup.main` runs Click's superclass in non-standalone mode and
  converts the parse-time `ClickException` to the JSON envelope under
  `--json`. This is a separate boundary from `handle_errors`, so the
  reasoning above — that `handle_errors` itself cannot see parse-time errors
  — still stands; see the amendment note.)*
