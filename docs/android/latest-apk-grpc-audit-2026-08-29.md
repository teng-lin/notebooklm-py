# Latest signed APK gRPC absence audit

**Audit date:** 2026-08-29  
**Package:** `com.google.android.apps.labs.language.tailwind`  
**Version:** `1.55.10.971450265` (`versionCode=153888`)  
**Target:** Android arm64; Dart `3.14.0-166.0.dev`

This audit tests whether a newer official-signed client can close the seven implemented gRPC
signature exceptions that are absent from the older `1.46.7.940945420` AOT image. It does not use
successful wire decoding as proof of a protobuf fully qualified name (FQN).

## Acquisition and signing identity

APKMirror's download gate returned HTTP 403, so `apkeep 1.0.0` acquired the XAPK through its
`apk-pure` source. The [Google Play listing](https://play.google.com/store/apps/details?id=com.google.android.apps.labs.language.tailwind)
and [APKMirror's arm64 release page](https://www.apkmirror.com/apk/google-inc/google-notebooklm/gemini-notebook-1-54-8-967070991-release/gemini-notebook-1-54-8-967070991-android-apk-download/)
provide independent package/publisher and signing-certificate metadata.

Android `apksigner` verifies every split under APK Signature Scheme v3 and verifies the source
stamp. The signer is `CN=Android, OU=Android, O=Google Inc., L=Mountain View, ST=California, C=US`.

| Identity | SHA-256 |
|---|---|
| APK signer certificate | `ba49176908275f83be9ae1034968f0b18e65177a64e5a40b3a621f148dfb6fa2` |
| source-stamp signer certificate | `3257d599a49d2c961a471ca9843f59d341a405884583fc087df4237b733bbd6d` |
| XAPK | `67fafb471ffa50379b36e8a33879292a7ff52eb04f4460d33f54db28dc51ad78` |
| base APK | `6552f1192135ccd83ebd0feb62534f842d6697c99be8855b847ca5ead544c81a` |
| arm64 split APK | `8206eb94fe01620f4388a141cdff3a07690bf06029e1277a763a1ca7ece80698` |
| Dart AOT image | `77bff7507e393c092b78ff1756bb3d726881050b22728dcc8c46cf0fecd7cda7` |
| `libflutter.so` | `ee1d2af8af0f80dea3f5c605d6537cfd2d80a28912eeb39a6ab918725d0f671c` |
| acquisition metadata response | `70cecd9b821842883e26fdf76bf1bd0080742147d1be5e4cd03027b60c79a525` |

The source stamp reports `2026-08-28T17:51:46Z`. The APK certificate hash matches the certificate
published independently on APKMirror.

## Blutter result

Blutter completed in an isolated temporary copy. Two code-analysis warnings were nonfatal; the
object pool, IDA naming script, and reconstructed generated protobuf clients were produced.

| Evidence | SHA-256 |
|---|---|
| `blutter-out/pp.txt` | `c2b64fd7d08a64f833b343f54bc697520096dfaef10740ebcbcd66a5c8e24b9a` |
| `blutter-out/ida_script/addNames.py` | `b75adf9f8bb92085c853dd30d231d533aec987024cf0e442f7cafc03dab24518` |
| [53-path inventory](../../tests/fixtures/android/latest_apk_grpc_paths.txt) | `b5df4996f271e71ccc14e0ae0f8eaa13e1e337b4bc726b54a487a0c4f6d31697` |
| [52 exact signatures](../../tests/fixtures/android/latest_apk_grpc_signatures.csv) | `6381163929c18d51eb654bc677846061ea65e9d501b9beb9db3952b749b32b7c` |

`scripts/extract_blutter_grpc_signatures.py` recovered 52 exact generated-client bindings. The
binary contains 47 orchestration full paths: 46 have adjacent exact request/response generic
bindings, while `UpsertArtifactUserState` remains the sole present orchestration path without one.

Compared with the older `1.46.7.940945420` binary, this build adds exact bindings for
`CancelGeneration`, `ListArtifactScheduledNotificationConfigs`,
`UpdateArtifactScheduledNotificationConfig`, `DiscoveryService/BatchSearchNotebooks`, and
`DiscoveryService/SearchNotebooks`. Its two-method `DiscoveryService` replaces the old
`LabsTailwindDiscoveryService/PrototypeNotebookSearch` call site. The committed inventories are
version-scoped so this delta cannot silently change the older capture narrative.

## Seven-method absence result

Each target was searched as a method name and full-path suffix across the decompressed XAPK, AOT
image, native libraries, Dex/resources, `pp.txt`, `addNames.py`, and reconstructed Dart output.

| RPC | AOT image | `pp.txt` | `addNames.py` | Dex/resources/native tree |
|---|---:|---:|---:|---:|
| `CopyProject` | 0 | 0 | 0 | 0 |
| `MutateSource` | 0 | 0 | 0 | 0 |
| `GenerateReportSuggestions` | 0 | 0 | 0 | 0 |
| `CreateLabel` | 0 | 0 | 0 | 0 |
| `MutateLabel` | 0 | 0 | 0 | 0 |
| `DeleteLabels` | 0 | 0 | 0 | 0 |
| `CancelDiscoverSourcesJob` | 0 | 0 | 0 | 0 |

The newer signed client therefore cannot supply exact request/response FQNs for these methods.
They were tree-shaken or never compiled into the application. The seven runtime implementations
remain explicit signature exceptions; promoting them into a generated service would be a package
guess, not extraction.

## Reproduction

The complete audit workspace was retained at `/tmp/notebooklm-apk-audit.aTRjvM` on the audit host.
It is temporary evidence; the hashes above are the durable identity boundary.

```bash
/tmp/notebooklm-apk-audit.aTRjvM/apkeep-src/target/release/apkeep \
  -a com.google.android.apps.labs.language.tailwind \
  -d apk-pure \
  /tmp/notebooklm-apk-audit.aTRjvM/downloads

/Users/blackmyth/Library/Android/sdk/build-tools/36.0.0/apksigner \
  verify --verbose --print-certs \
  /tmp/notebooklm-apk-audit.aTRjvM/xapk/com.google.android.apps.labs.language.tailwind.apk

/Users/blackmyth/src/blutter-nblm/.venv/bin/python \
  /tmp/notebooklm-apk-audit.aTRjvM/blutter/blutter.py \
  /tmp/notebooklm-apk-audit.aTRjvM/blutter-input \
  /tmp/notebooklm-apk-audit.aTRjvM/blutter-out

uv run python scripts/extract_blutter_grpc_signatures.py \
  /tmp/notebooklm-apk-audit.aTRjvM/blutter-out/pp.txt \
  /tmp/notebooklm-apk-audit.aTRjvM/blutter-out/ida_script/addNames.py
```
