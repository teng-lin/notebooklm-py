# Android labels and collections: live validation

**Status:** Live-verified complete CRUD and membership wire lifecycle

**Last verified:** 2026-08-29

This report reconciles the earlier 2026-08-13 rejected-probe ledger with the later Phase B audit.
The probe used the `ng-master` profile's durable credential only to mint an in-memory Android
bearer. It called the fixed `notebooklm-pa.googleapis.com` gRPC authority through the headless
reproducer in `/Users/blackmyth/src/gemini-notebook-mobile`; no browser was opened. Credential and
account values were neither printed nor persisted.

## Read modes

`GetLabels` succeeded in both exact modes:

| Resource | Request | Response projection |
| --- | --- | --- |
| Source labels | owned notebook UUID at request field `#2` | repeated label records at response field `#1` |
| Notebook collections | collection discriminator `3` at request field `#3` | repeated collection records at response field `#2` |

The source-label read used a current owned notebook rather than the stale profile fixture that
correctly returned `NOT_FOUND`. The replacement read returned an empty label set. The account-level
collection read returned an empty collection set.

## Disposable source-label lifecycle

Two disposable labels were exercised and removed:

1. `CreateLabel` created an empty uniquely named label. A `GetLabels` read-back found exactly one
   new canonical ID.
2. `MutateLabel` changed its name and emoji in one property operation. Read-back returned both
   requested values.
3. `DeleteLabels` removed the exact ID. A final `GetLabels` returned an empty set.
4. A second `CreateLabel` created a label with one existing source member.
5. One `MutateLabel` operation added a second source; read-back preserved the first and appended the
   second.
6. A second `MutateLabel` operation removed the first source; read-back retained only the second.
7. `DeleteLabels` removed the exact disposable label.

Membership requests intentionally sent one member per RPC. The server accepts a repeated-looking
operation shape but the later audit proved only the first member is applied; the public Web API
already performs one mutation per member and exposes partial/non-atomic batch semantics.

## Disposable collection lifecycle

One disposable collection was created, mutated, and removed:

1. `CreateLabel` with collection discriminator `3` created a uniquely named collection containing
   one existing notebook ID. `GetLabels` read-back returned the new canonical collection ID.
2. `MutateLabel` with discriminator `3` changed the collection name and emoji; both matched on
   read-back.
3. One member mutation added a second notebook ID; read-back contained both IDs.
4. One member mutation removed the first notebook ID; read-back retained only the second.
5. `DeleteLabels` with collection discriminator `3` removed the exact disposable collection.

All created labels and collections were deleted. These operations change only the grouping
resources; the referenced sources and notebooks were not modified or deleted.

## Evidence precedence and implementation boundary

The committed 2026-08-13 comparison manifest records unsuccessful candidate shapes for
`CreateLabel` and `UNIMPLEMENTED` responses for `MutateLabel`/`DeleteLabels`. This 2026-08-29 run
uses the later wire shapes retained by the Phase B reproducer and demonstrates successful
create/read-back, property mutation, membership mutation, and delete lifecycles on current owned
resources. Under the Phase B evidence-precedence rule, this later valid-resource success supersedes
the older rejected probes for these exact shapes.

Implementation may therefore admit `GetLabels`, manual `CreateLabel`, property and one-member
`MutateLabel`, and `DeleteLabels` for both source labels and notebook collections. AI-generated
label creation remains separate: this run validated only the manual-create branch and must not be
used to infer the auto-label request union.

## Public adapter qualification

After assembly selected `AndroidCollectionsAPI` for explicit `backend="android"`, the permanent
`tests/e2e/test_android_collections_conformance.py` gate passed twice independently against the
same isolated `ng-master` profile. Each run created a fresh disposable Web-backed notebook and an
Android-backed collection, then exercised the complete public namespace: `list`, `get_or_none`,
`get`, `notebooks`, `create`, `rename`, `add_notebooks`, `remove_notebooks`, and `delete`.

Both runs verified the mixed-backend membership expansion, deleted the exact collection ID, and
deleted the exact disposable notebook ID in `finally`. No collection or notebook created by either
run remained. No browser was opened; the selected lifecycle loaded the profile's master token only
during async open and kept gRPC channel construction lazy until the first collection call.
