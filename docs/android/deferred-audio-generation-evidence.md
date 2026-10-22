# Deferred Audio Overview generation (#2505)

**Investigation date:** 2026-10-21

The public audio creation API accepts `later=True`, and the CLI accepts
`notebooklm generate audio --later`. The provider chooses the execution time;
the UI describes a delay of a few hours. Deferred generation remains subject
to account feature availability and quota. CLI `--later --wait` is rejected.

## Web evidence

The authenticated eager bundle (5,336,656 bytes, SHA-256
`0951c3ce7dc41d1381abca827de74bf1e96cfa5d70ca545177554ef492d708a6`)
and notebook lazy module `DR4Ugf` (SHA-256
`d69d885559f52d13b500046cdd1bfa7edbf0d05ada8dbc7c86aa2df89c0e188c`)
trace the audio customization dialog's `isDeferredGeneration` result through
`sNe` and the create-artifact effect into `DM.g9a`.

`DM.g9a` invokes `SQb` when the deferred argument and flag `45826277` are true.
`SQb` sets a nested boolean field `1` to true and places that message at
`CreateArtifactRequest` field `7`. The ordinary `R7cb6c` creation RPC is used:

```text
[RequestContext, notebook_id, audio_artifact, null, null, null, [true]]
```

The corresponding protobuf suffix is `3a 02 08 01`. The remote FQN of the
field-7 message remains unknown, so Android represents it with repository-local
`WireCreateArtifactDeferredProjection` and `WireDeferredGenerationOptions`
messages, merged into the existing exact `CreateArtifactRequest`.
Immediate creation omits field 7 entirely.

The response remains `CreateArtifactResponse.artifact #1`. Its status is
`ARTIFACT_STATUS_SCHEDULED = 7`, already independently recovered in
[`enums.txt`](enums.txt). The library exposes it as `scheduled`, with
`Artifact.is_scheduled` and `GenerationStatus.is_scheduled`. It is nonterminal;
callers may explicitly poll until generation completes. The archived schema
also names `Artifact.scheduledExecutionTime #34`; this change does not add a
public timestamp projection.

The separately registered `NTlrje ScheduleGenerateArtifact` schedules an
existing suggested artifact, with context `#1` and artifact ID `#2`. It is not
needed to create deferred audio. `nRZAQd ExecuteDeferredGeneration` has no
consumer call site in this capture. Scheduled-notification configuration RPCs
manage notifications and are not the creation action.

## Android static evidence

An XAPK acquired through the documented `apkeep` / `apk-pure` method reports
version `1.60.8.990840121`, version code `163470`. Its arm64 AOT image
`libNotebookLM_prod_android_library_flutter_artifacts.so` has SHA-256
`1dc69b393029fdcbf18f22af9d2f3f29154d5aaef998ccc1ea9828673bafe577`.
It contains `isDeferredGeneration`, `onGenerateLater`, `withDeferredGeneration`,
`DeferredCustomizationResult`, the Generate later localization, and the exact
`GetArtifactDeferredGenerationQuota` gRPC path. This is static supporting
evidence; the acquired package's publisher signature was not verified during
this investigation, and no exact remote field-7 message FQN is claimed.

Earlier [#2283 quota probes](https://github.com/teng-lin/notebooklm-py/issues/2283#issuecomment-5515959984)
show that the initial deferred-quota permission denial was superseded by
`[] -> [true]` on Standard and Pro. A historic denial must not be treated as
permanent Android capability evidence.

## Live qualification

The reproducible test is
[`tests/e2e/test_deferred_audio.py`](../../tests/e2e/test_deferred_audio.py).
Each backend creates its own disposable notebook and ready text source, sends
one deferred audio creation, verifies the returned status and independent
artifact read-back and polling, then deletes the artifact and notebook.
The test requires `scheduled`; an immediate-generation success is not accepted
as proof of scheduling. It does not wait hours for worker execution.

On 2026-10-21 both parametrized cases passed against the live service
(2 passed in 13.87 seconds): Web and native Android each returned and persisted
status 7; polling preserved `scheduled`, and artifact deletion was confirmed
before notebook cleanup.

Run against a dedicated authenticated profile with Web cookies and an Android
master token:

```bash
NOTEBOOKLM_PROFILE=YOUR_TEST_PROFILE NOTEBOOKLM_DEFERRED_AUDIO_CONFORMANCE=1 \
  uv run pytest -o addopts='' \
  tests/e2e/test_deferred_audio.py -v
```

## Server-selected execution time

Two retained overviews created at approximately 19:49:33 UTC on 2026-10-21
received these timestamps in `Artifact.scheduledExecutionTime #34`:

| Creation backend | Scheduled execution (UTC) | Independent observation |
| --- | --- | --- |
| Android | 19:53:50 | `scheduled -> in_progress` observed at 19:54:05 |
| Web | 20:11:55 | Retained for automatic execution monitoring |

The Android job started without a retry, `GenerateArtifact`, or
`ExecuteDeferredGeneration` call. These observed delays were about 4 and
22 minutes, respectively; the UI's "a few hours" text is not a fixed delay.
The continued execution and cleanup record is attached to
[PR #2506](https://github.com/teng-lin/notebooklm-py/pull/2506).

A bounded attempt to move the retained Web artifact forward with
`UpdateArtifact` and mask `scheduled_execution_time` returned
`INVALID_ARGUMENT`; independent read-back kept the original timestamp.
A separate creation probe set artifact field 34 to one minute ahead while
still enabling deferred generation. The server returned `scheduled` but
ignored that timestamp, choosing about five hours later instead. The extra
probe artifact was deleted immediately. These observations support exposing
`later` as a boolean, without promising an arbitrary caller-selected time.
