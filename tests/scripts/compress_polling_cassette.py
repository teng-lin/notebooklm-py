#!/usr/bin/env python3
"""Compress the artifacts polling cassette after recording.

The live ``artifacts_poll_rename_wait.yaml`` cassette is recorded by
``tests/integration/test_polling_vcr.py::TestPollingReplay::test_poll_rename_wait``
running with ``NOTEBOOKLM_VCR_RECORD=1``. The raw recording can include
50-100+ ``LIST_ARTIFACTS`` interactions because the live API takes 30-60 s
to finish a flashcard generation and ``wait_for_completion`` polls at
exponential intervals (1 s → 2 s → 4 s → ... → 5 s cap) until status
flips to ``COMPLETED``.

We do not need every poll response on disk — the replay test only needs
enough interactions to exercise the polling loop. This script keeps:

* The first ``LIST_ARTIFACTS`` interaction — consumed by the explicit
  ``poll_status`` call in the test.
* The next ``KEEP_PROCESSING`` ``LIST_ARTIFACTS`` interactions — exercise
  the ``wait_for_completion`` backoff path while the artifact is still
  ``in_progress``.
* The first ``LIST_ARTIFACTS`` interaction that reports
  ``status == COMPLETED`` — terminates the wait loop.
* The single ``CREATE_ARTIFACT`` and ``RENAME_ARTIFACT`` interactions
  that bookend the chain.

Run from the repo root::

    uv run python tests/scripts/compress_polling_cassette.py

The script is idempotent — running it on an already-compressed cassette
is a no-op because no PROCESSING interactions exist beyond the kept
prefix. It writes the result back to the same file path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTE_PATH = REPO_ROOT / "tests" / "cassettes" / "artifacts_poll_rename_wait.yaml"

# RPC IDs (mirror src/notebooklm/rpc/types.py — duplicated here so this script
# can run without importing the notebooklm package).
RPCID_CREATE_ARTIFACT = "R7cb6c"
RPCID_LIST_ARTIFACTS = "gArtLc"
RPCID_RENAME_ARTIFACT = "rc3d8d"

# How many in-progress LIST_ARTIFACTS responses to retain after the first
# (explicit poll_status) one. 3 in-progress + 1 completed gives the
# ``wait_for_completion`` loop four iterations to walk, comfortably above
# the ``MIN_POLLING_INTERACTIONS = 3`` floor enforced by the test.
KEEP_PROCESSING = 3


def _rpcid(interaction: dict) -> str | None:
    qs = parse_qs(urlparse(interaction["request"]["uri"]).query)
    rpcids = qs.get("rpcids", [])
    return rpcids[0] if rpcids else None


def _is_completed(interaction: dict, task_id: str) -> bool:
    """True if this LIST_ARTIFACTS response reports the task as COMPLETED.

    The artifact array shape is roughly::

        [task_id, title, type, source_ids, status, ...]

    where ``status == 3`` is ``COMPLETED`` (see ``ArtifactStatus`` in
    ``src/notebooklm/rpc/types.py``). We don't fully decode the WRB
    envelope here — a simple substring check on ``,3,`` immediately after
    the task block is robust enough for this compression step.
    """
    body = interaction["response"]["body"]["string"]
    if task_id not in body:
        return False
    idx = body.find(task_id)
    chunk = body[idx : idx + 400]
    # Status code 3 appears after the source_ids triple. A bare ``,3,`` in
    # the chunk is the COMPLETED marker; PROCESSING is ``,1,``.
    return ",3," in chunk


def _extract_task_id(cassette: dict) -> str:
    """Pull the task_id back out of the recorded CREATE_ARTIFACT response."""
    for interaction in cassette["interactions"]:
        if _rpcid(interaction) != RPCID_CREATE_ARTIFACT:
            continue
        body = interaction["response"]["body"]["string"]
        # The CREATE_ARTIFACT response carries the task_id as the first
        # quoted string in the inner JSON. We scan from the wrb.fr envelope
        # for the first ``\"...\"`` token. The inner JSON is itself a
        # JSON-encoded string, so quote characters are backslash-escaped:
        # ``[["wrb.fr","R7cb6c","[[\"<task_id>\", ...`` (two leading ``[``)
        # or ``[\"<task_id>\"`` depending on the response shape — both
        # start the same way once we look past the leading ``[`` runs.
        envelope = '"' + RPCID_CREATE_ARTIFACT + '","'
        env_idx = body.find(envelope)
        if env_idx == -1:
            continue
        # First escaped quote after the envelope opens the inner JSON
        # string. The task_id is whatever sits between that ``\"`` and the
        # next ``\"``.
        first_quote = body.find('\\"', env_idx)
        if first_quote == -1:
            continue
        start = first_quote + 2  # skip the ``\"`` escape
        end = body.find('\\"', start)
        if end == -1:
            continue
        return body[start:end]
    raise RuntimeError(
        "Could not locate task_id in CREATE_ARTIFACT response — has the "
        "response shape drifted? Re-record and inspect the raw cassette."
    )


def compress(cassette_path: Path = CASSETTE_PATH) -> tuple[int, int]:
    """Compress ``cassette_path`` in place. Returns ``(before, after)`` counts."""
    with cassette_path.open(encoding="utf-8") as fh:
        cassette = yaml.safe_load(fh)

    interactions = cassette["interactions"]
    before = len(interactions)

    task_id = _extract_task_id(cassette)

    create_idx: int | None = None
    rename_idx: int | None = None
    list_processing: list[int] = []
    list_completed: int | None = None

    for i, interaction in enumerate(interactions):
        rpc = _rpcid(interaction)
        if rpc == RPCID_CREATE_ARTIFACT and create_idx is None:
            create_idx = i
        elif rpc == RPCID_RENAME_ARTIFACT and rename_idx is None:
            rename_idx = i
        elif rpc == RPCID_LIST_ARTIFACTS:
            if list_completed is None and _is_completed(interaction, task_id):
                list_completed = i
            elif list_completed is None:
                list_processing.append(i)

    if create_idx is None:
        raise RuntimeError("No CREATE_ARTIFACT interaction found in cassette")
    if rename_idx is None:
        raise RuntimeError("No RENAME_ARTIFACT interaction found in cassette")
    if list_completed is None:
        raise RuntimeError("No COMPLETED LIST_ARTIFACTS interaction found in cassette")

    # Keep the first (1 + KEEP_PROCESSING) processing polls plus the completed one.
    # The first one is consumed by the explicit poll_status call in the test;
    # the rest drive wait_for_completion's iteration loop.
    keep_processing = list_processing[: 1 + KEEP_PROCESSING]

    keep_indices = sorted({create_idx, *keep_processing, list_completed, rename_idx})
    cassette["interactions"] = [interactions[i] for i in keep_indices]

    with cassette_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            cassette,
            fh,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    return before, len(cassette["interactions"])


def main() -> int:
    if not CASSETTE_PATH.exists():
        print(f"ERROR: cassette not found at {CASSETTE_PATH}", file=sys.stderr)
        print(
            "Record it first with: NOTEBOOKLM_VCR_RECORD=1 uv run pytest "
            "tests/integration/test_polling_vcr.py",
            file=sys.stderr,
        )
        return 1
    before, after = compress()
    print(f"Compressed {CASSETTE_PATH.name}: {before} -> {after} interactions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
