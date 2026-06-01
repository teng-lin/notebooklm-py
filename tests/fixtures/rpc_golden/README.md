# RPC golden payload fixtures

One JSON file per `notebooklm.rpc.types.RPCMethod` enum member. These files
are the on-disk goldens consumed by
`tests/unit/test_rpc_golden_payloads.py`.

Adding a new `RPCMethod` requires adding a fixture here, or the test suite
fails with `Missing golden fixtures for: [...]`. Renaming or removing an
enum member requires deleting (or renaming) the corresponding fixture file,
or the orphan check fails with `Orphan fixtures with no corresponding
RPCMethod member: [...]`.

## File naming

`<METHOD_NAME>.json` — using the **enum name** (e.g. `LIST_NOTEBOOKS.json`,
not `wXbhsf.json`). This keeps the fixture human-readable and survives wire
ID changes without renaming the file.

## Schema

```json
{
  "method_name": "LIST_NOTEBOOKS",
  "method_id": "wXbhsf",
  "description": "One sentence describing what this RPC does.",

  "request": {
    "params": [/* python list passed to encode_rpc_request */],
    "expected_f_req": [[[/* exact triple-nested array the encoder must produce */]]]
  },

  "response": {
    "chunks": [
      [["wrb.fr", "<method_id>", "<json-encoded-result-string>", null, null, null, "generic"]]
    ],
    "allow_null": false,
    "expected_decoded": /* the python payload decode_response must return */
  },

  "mapper": "notebooklm._research_task_parser:parse_research_task_models",
  "mapper_expected": [/* the public-dict shape (or list thereof) produced by the mapper */],

  "drift_cases": [
    {
      "name": "error_frame",
      "description": "One sentence describing the drift scenario.",
      "chunks": [/* synthetic chunks for this scenario */],
      "allow_null": false,
      "expected_exception": "RPCError",
      "expected_message_substring": "..."
    },
    {
      "name": "multi_frame_placeholder_then_final",
      "chunks": [/* null placeholder frame, then populated frame */],
      "expected_decoded": /* the python payload decode_response must return */
    }
  ]
}
```

`mapper` and `mapper_expected` are **optional** — omit them for methods
whose payload is consumed by inline feature-level extraction
(`safe_index`-based) rather than a centralised mapper. When present, the
mapper is imported via `module.path:attribute` form and applied to
`expected_decoded`; the returned value (or its `to_public_dict()` form for
dataclasses) is compared against `mapper_expected`.

`drift_cases` is **optional** — present only on the drift-prone methods
(`CREATE_ARTIFACT`, `ADD_SOURCE`, `START_FAST_RESEARCH`, `LIST_NOTEBOOKS`)
that pin the decoder's *error-path* behaviour in addition to the happy path.
Each case rebuilds the chunked wire response from its own `chunks` and feeds
it to `decode_response`. A case must declare **exactly one** of:

- `expected_exception` — the exact decoder exception class name
  (`RPCError`, `ClientError`, `RateLimitError`, or `UnknownRPCMethodError`),
  optionally narrowed by a case-insensitive `expected_message_substring`.
  Covers `er` frames, embedded `UserDisplayableError` rate limits, bare
  gRPC NOT_FOUND/PERMISSION_DENIED status codes, and method-id drift.
- `expected_decoded` — the payload `decode_response` must return when the
  response carries multiple frames for one id (e.g. a `rt=c` null
  placeholder followed by the populated final frame). Pins the
  "prefer the last non-null `wrb.fr` frame" contract.

`allow_null` defaults to `false` per case.

## Scrubbing rules

All identifiers in these fixtures are **synthetic** and obviously scrubbed.
This avoids any leak risk and is the simpler alternative to the
cassette-scrubber pipeline documented in
[ADR-0006](../../../docs/adr/0006-vcr-scrubber-strategy.md).

| Class | Placeholder example |
|---|---|
| Notebook ID | `SCRUBBED_NB_<NNN>` |
| Source ID | `SCRUBBED_SRC_<NNN>` |
| Artifact ID | `SCRUBBED_ARTIFACT_<NNN>` |
| Conversation ID | `SCRUBBED_CONV_<NNN>` |
| Note ID | `SCRUBBED_NOTE_<NNN>` |
| Research task ID | `SCRUBBED_RESEARCH_<NNN>` |
| Title / text / prompt | `Scrubbed <noun>` |
| URL | `https://scrubbed.example.com/<path>` |
| Email | `scrubbed.user@example.invalid` |
| Account user index | `0` |

No real credentials, no real notebook IDs, no real account identifiers.
The pre-commit `check_cassettes_clean.py` guard does not scan this
directory because these fixtures are synthetic by construction — no
recording pipeline produces them.

## How the chunked response is reconstructed

The test reassembles the chunked wire format from `response.chunks` like
this:

```text
)]}'
<byte_count_of_chunk_1_json>
<chunk_1_json>
<byte_count_of_chunk_2_json>
<chunk_2_json>
...
```

The byte count is the UTF-8 byte length of the chunk's compact JSON
serialisation. The reconstructed body is fed to
`notebooklm.rpc.decoder.decode_response`, which strips the anti-XSSI
prefix, parses the chunks, and extracts the result for the requested RPC
ID.

## Why goldens and not VCR cassettes

These fixtures cover the **shape** of the request envelope and the decoder
output. A live cassette would also cover transport-layer details (headers,
cookies, status codes) but at the cost of dragging the cassette-scrubber
pipeline into a unit-level concern. The shape-only goldens here are
sufficient to catch:

- Enum-value drift in `RPCMethod` (each fixture pins the wire ID).
- Encoder format drift (triple-nesting, JSON-compactness, trailing
  marker).
- Decoder format drift (anti-XSSI prefix, chunk parsing, JSON-decoding of
  the inner result string, null-result handling).
- Downstream mapper drift, where a mapper is documented.

Live transport coverage is the job of the integration / VCR test suites,
not this module.
