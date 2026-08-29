# Android notebooks: live validation

**Status:** Live-verified partial namespace

**Last verified:** 2026-08-28

This report records a headless Android-backend validation against one disposable owned notebook.
The probe read the `ng-master` profile's durable credential through `ProfileStore`, minted a
short-lived bearer in memory, and used the private `AndroidSession`. It printed only boolean
outcomes and gRPC status codes. Credentials, account identity, bearer text, and resource IDs were
neither logged nor persisted.

## Authentication control

Before mutation testing, the bearer completed
`LabsTailwindOrchestrationService/GetOrCreateAccount` with gRPC status `0`. The response carried 69
serialized bytes. This proves backend acceptance rather than merely successful token issuance.

## Disposable-notebook sequence

The probe created a uniquely titled notebook through `CreateProject`, confirmed its ID appeared in
`ListRecentlyViewedProjects`, exercised the candidate mutations below, and deleted the notebook in
a `finally` block. Cleanup completed successfully.

| Operation | Request evidence | Result |
|---|---|---|
| Set emoji | `MutateProjectRequest.project_id #1`; repeated mutation `#2`; `change_property #4`; repository-local candidate `new_emoji #3` | bare `Project` response and `GetProject` read-back both returned the requested emoji |
| Clear emoji | same shape, with an explicitly encoded zero-length string at `new_emoji #3` | bare response and read-back both returned the empty string |
| Set title and emoji together | `new_title #2` and `new_emoji #3` in the same change-property message | both fields matched in the bare response and read-back |
| Remove from recent | exact `project_id #1` request with no fabricated context | gRPC `INTERNAL` (`13`) |
| Final cleanup | `DeleteProjects` with the exact disposable ID | success |

The emoji candidate was deliberately encoded as a repository-local wire overlay. The successful
round trips prove tag and semantics, but do not turn the repository-local field name into a claim
about an unrecovered Google descriptor.

## Promotion boundary

Emoji set, clear, and combined update are admitted for implementation after the serialized fixture,
ledger entry, and non-replay/read-back tests are checked in. `RemoveRecentlyViewedProject` remains
unsupported: this fresh-notebook run rules out the earlier copied-notebook setup as the explanation
for status `13`, but it still does not reveal a successful mobile response contract. Web success is
not a substitute for Android namespace conformance.
