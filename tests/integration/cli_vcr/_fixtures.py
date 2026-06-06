"""Placeholder ids for the ``cli_vcr`` suite (issue #1452).

Why a *flat* placeholder set (and not a recorded-vs-replay registry)
--------------------------------------------------------------------
These tests run the real CLI -> Client -> RPC path, but VCR replays HTTP from
``tests/cassettes/*.yaml``. The matcher (``tests/vcr_config.py`` —
``_rpcids_matcher`` + ``_freq_body_matcher``) selects a cassette by the RPC
method id (``rpcids=``) **plus the decoded body *shape***, and **never** by the
notebook/source id in the request. The 105 cassettes were recorded against 15
distinct notebooks; ``mock_context`` injects a single id (``c3f6285f``)
regardless of which notebook a cassette was recorded against, and replay
succeeds anyway (e.g. ``test_artifacts`` replays
``artifacts_list_flashcards.yaml`` — recorded vs ``167481cd`` — while passing
``c3f6285f``).

The practical upshot: **the ids below are decorative placeholders.** Only a
valid id *shape* (a 36-char UUID where the CLI's resolver expects one, so it
short-circuits the ``LIST_NOTEBOOKS`` / ``LIST_SOURCES`` preflight) matters; the
specific value is never matched against the recorded request. That is why there
is no canonical-fixture registry and no cassette-membership guard — they would
police a relationship that does not exist.

The one place a placeholder is load-bearing is the **input-echo** assertion: a
mutation command threads the id the test passed into its own ``--json`` output,
so ``output["notebook_id"] == MUTATION_NOTEBOOK_ID`` holds for *any* cassette
and survives any re-record. ``MUTATION_NOTEBOOK_ID`` is deliberately a value
present in **zero** cassettes, which proves the echo comes from the input.

Keeping the current literal values avoids churn — they are arbitrary, so there
is no reason to change them.
"""

from __future__ import annotations

# --- Read-only / context placeholders -------------------------------------
# ``mock_context`` writes ``PLACEHOLDER_NOTEBOOK_ID`` into the CLI context file.
PLACEHOLDER_NOTEBOOK_ID = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"
PLACEHOLDER_SOURCE_ID = "fdfc8ac4-3237-4f2a-8a79-3e24297a7040"

# --- Per-family read placeholders -----------------------------------------
# Notebooks whose cassettes happen to need a specific full UUID on the command
# line (so the resolver short-circuits); the value is still decorative.
CHAT_NOTEBOOK_ID = "f59447f4-2a13-4d64-9df8-bc89c615c7bd"
ARTIFACT_NOTEBOOK_ID = "f7d1e2b6-2334-4016-b81d-aded7b3fa9b6"

# --- Generate / mind-map placeholders -------------------------------------
GENERATE_NOTEBOOK_ID = "bb00c9e3-656c-4fd2-b890-2b71e1cf3814"
GENERATE_SOURCE_ID = "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad"
# ``revise-slide`` passes obviously-synthetic ids; the matcher ignores them.
GENERATE_PLACEHOLDER_NOTEBOOK_ID = "00000000-0000-0000-0000-000000000000"
GENERATE_PLACEHOLDER_SOURCE_ID = "00000000-0000-0000-0000-000000000001"

# --- Source-delete placeholders -------------------------------------------
DELETE_SOURCE_ID = "ff503bfa-5e39-4281-a1d8-2a66c7b86724"
DELETE_NOTEBOOK_ID = "06f0c5bd-108f-4c8b-8911-34b2acc656de"

# --- Input-echo placeholder ------------------------------------------------
# Present in ZERO cassettes: a mutation's ``--json`` output echoes whatever id
# the test passed, so comparing the echoed id to this value proves the CLI
# threaded the *input* through (re-record-safe; see module docstring).
MUTATION_NOTEBOOK_ID = "b8d6f2a1-4c3e-4a9b-8f7d-1e2c3a4b5c6d"

# --- Back-compat aliases ---------------------------------------------------
# ``conftest`` historically exported these names; keep them as aliases so
# existing imports keep resolving without re-introducing inline literals.
VCR_READONLY_NOTEBOOK_ID = PLACEHOLDER_NOTEBOOK_ID
VCR_READONLY_SOURCE_ID = PLACEHOLDER_SOURCE_ID
VCR_MUTABLE_NOTEBOOK_ID = MUTATION_NOTEBOOK_ID
VCR_SHARE_NOTEBOOK_ID = MUTATION_NOTEBOOK_ID

__all__ = [
    "ARTIFACT_NOTEBOOK_ID",
    "CHAT_NOTEBOOK_ID",
    "DELETE_NOTEBOOK_ID",
    "DELETE_SOURCE_ID",
    "GENERATE_NOTEBOOK_ID",
    "GENERATE_PLACEHOLDER_NOTEBOOK_ID",
    "GENERATE_PLACEHOLDER_SOURCE_ID",
    "GENERATE_SOURCE_ID",
    "MUTATION_NOTEBOOK_ID",
    "PLACEHOLDER_NOTEBOOK_ID",
    "PLACEHOLDER_SOURCE_ID",
    "VCR_MUTABLE_NOTEBOOK_ID",
    "VCR_READONLY_NOTEBOOK_ID",
    "VCR_READONLY_SOURCE_ID",
    "VCR_SHARE_NOTEBOOK_ID",
]
