# URL recovery evidence (#2110)

Verified on 2026-09-27 against Web and native Android, using temporary notebooks
that were deleted after each probe.

## Failure diagnostic

Adding `https://issue-2110-unresolvable.invalid/` fails with RPC code 9 and leaves
an ERROR source. Web reports `SourceAddError` with an `RPCError` cause; native
Android reports `ClientError`. The source workflow preserves that native error
and attaches the correlated `source_id` and commit stage as operation metadata.

The Web SourceSettings capture is:

```json
[null, 3, [null, null, null, null, null, null, [1]]]
```

The fetch diagnostic is nested at tags **3 → 7 → 1**, not a scalar directly at
tag 3. Native Android's unknown-field decoder also returned `1` for this failure.
The Android ERROR row had no URL, so URL equality alone cannot identify it.
The positively correlated tentative registration ID supplies that identity.
The diagnostic remains experimental; unknown shapes or values disable recovery.

The minimal Web capture is retained in
[`source_failure_settings.json`](../../tests/unit/fixtures/source_failure_settings.json).
Decoder regressions cover malformed, duplicate and wrong-wire-type fields.

## Android recovery and cleanup

A second native Android probe exercised the shared application workflow with
`fallback_fetch=True` and `cleanup_on_failure=True`. Only the external fetch was
replaced with a fixed text fixture: a deliberately nonexistent domain cannot
supply downloadable content.

These operations all used the live Android backend:

1. Register the tentative source and receive the failed URL commit.
2. Identify the new ERROR source by its correlated ID and diagnostic `1`.
3. Upload the replacement text source and wait for READY.
4. Recheck and delete the identified failed source.
5. List sources: exactly the replacement remained; cleanup reported `deleted`.

The temporary notebook was then deleted. This verifies the native backend
workflow, not an end-to-end successful public-page download. Fetch networking
is tested separately, including a real libcurl transfer with DNS pinning and
proxy isolation. Web cleanup remains disabled when the original operation has
no positively attributed source ID.
