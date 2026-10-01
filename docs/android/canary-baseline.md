# Android RPC canary baseline

**Last Updated:** 2026-10-01

`tests/fixtures/android/canary_baseline.json` records the response shapes and
unknown-field counts observed by `scripts/android_grpc_canary.py` against the
immutable CI template. Three consecutive protected-main RPC health runs agree:

- [2026-09-29](https://github.com/teng-lin/notebooklm-py/actions/runs/36535597781)
- [2026-09-30](https://github.com/teng-lin/notebooklm-py/actions/runs/36682698161)
- [2026-10-01](https://github.com/teng-lin/notebooklm-py/actions/runs/36829382365)

| RPC | Structural SHA-256 | Unknown fields |
| --- | --- | --- |
| `GetProject` | `367f83fb36bb3bc67b174baad75bebad298e40319e27ea03afed1cf91e1cda37` | 143 |
| `ListChatSessions` | `e6d6424e0fbcb1e967dba83ed719abf94fb78007b4d58dbc86c2b5c88756bc98` | 2 |

In all three runs, opening the Android session, refreshing the bearer, reading
the project ID, and listing the template's chat sessions succeeded. The canary
failed because its baseline was absent after the 2026-09-14 bootstrap deadline.
The matching fingerprints and counts establish a reproducible starting point;
they do not imply that the recovered protobuf schema covers every server field.

The fixture contains only structural hashes and counts. The canary continues to
fail on either kind of drift, a missing RPC entry, or a missing baseline. Review
new live diagnostics before changing these values; do not regenerate the
baseline automatically to clear a failing gate.
