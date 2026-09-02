# Android file transfer: live validation

**Status:** Live-verified

**Last verified:** 2026-08-29

> **Historical qualification snapshot.** The direct-upload outcomes below are
> retained as August 29 evidence. Follow-up work on August 31 removed the two
> Web upload collaborators by routing CSV/DOCX/PPTX through bounded Drive
> staging; current Android assembly has no Web operation collaborators. See
> [`web-compat-seam-closure.md`](web-compat-seam-closure.md#sourcesadd_file--the-mobile-upload-frontends-allowlist).

**App:** NotebookLM Android `1.46.7.940945420` (`versionCode=138238`)

This report records a successful official-app file upload, a successful headless replay of the
same mobile upload protocol, and successful mobile artifact downloads including slide PDF and
PPTX. Credentials, notebook IDs, source IDs, artifact IDs, capability URLs, resumable-session
values, and private artifact titles are deliberately omitted.

The original runnable reproducer, `scripts/reproduce_android_transfer.py`,
lived in a separate mobile-evidence workspace and is not part of this repository.
It reads a profile master token, mints a short-lived mobile OAuth bearer in memory, and never logs or
persists either credential.

## Result

| path | result | live evidence |
|---|---|---|
| Official Android app upload | success | 13,362-byte PDF appeared as a source and became ready |
| Headless mobile upload replay | success | same PDF uploaded through gRPC + Scotty; process exited 0 after source-ready polling |
| Headless mobile artifact download | success | 4,721,650-byte PNG, 1536×2752, valid PNG signature |
| Android public slide PDF download | success | 15,017,608 bytes, `application/octet-stream`, valid `%PDF-` signature |
| Android public slide PPTX download | success | 17,392,113 bytes, `application/octet-stream`, valid OOXML ZIP containing `[Content_Types].xml` and `ppt/` entries |
| Slide auth control without bearer | not an artifact | initial request returned HTTP 302 `text/html`; the same URL with the mobile bearer returned the bytes directly |

The disposable notebook created for the test was deleted after validation.

## File-format qualification

At this report's August 29 checkpoint, the public Sources namespace was the Android adapter and its
`add_file` implementation used native Android tentative registration plus Scotty except for two
extension-qualified compatibility calls:

| extension | native Android live result | public Android selection |
|---|---|---|
| `.pdf` | `READY` with PDF type | native Android upload |
| `.md` | `READY` with Markdown type | native Android upload |
| `.csv` | Scotty returned `final`; `GetProject` then returned type `UNKNOWN`, status `ERROR`, and no populated error-detail metadata | existing authenticated Web `add_file` collaborator |
| `.docx` | Scotty returned `final`; both OOXML and `application/msword` MIME probes then returned type `UNKNOWN`, status `ERROR`, and no populated error-detail metadata | existing authenticated Web `add_file` collaborator |

The CSV and DOCX outcomes reproduced with substantive content, including an OS-generated DOCX, so
they are not minimum-fixture validation failures. The same fixtures reached `READY` through the Web
uploader. Native `AddSources` with `CONTENT_TYPE_CSV` was also tested, but the backend stored it as
`TEXT`/pasted text rather than a CSV file source; the public adapter therefore does not coerce CSV
through that route. No compatibility routing is inferred for other extensions.

## The upload detail that caused the 404s

The Scotty start endpoint is:

```text
POST https://notebooklm-pa.googleapis.com/upload/upload/{project_id}
```

The path suffix is the **project/notebook ID**. It is not the source ID, and the bare
`/upload/upload/` endpoint is not valid.

This was the missing runtime detail. Static APK recovery established the host and base path, but
not the meaning of the appended UUID. Live comparison gave these outcomes:

| attempted path | result |
|---|---|
| production `/upload/upload/` | HTTP 404 |
| production `/upload/upload/{source_id}` | HTTP 404 |
| production `/upload/upload/{project_id}` | HTTP 200, upload status `active` |
| autopush base endpoint | HTTP 403, internal-access/ÜberProxy rejection |

The production 404 was unchanged over HTTP/1.1 and HTTP/2 when the path suffix was wrong. The
official app's working request used HTTP/1.1.

## Upload sequence

### 1. Register the tentative source

The app sends an HTTP/2 unary gRPC call:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/AddTentativeSources
```

The request includes the notebook ID, file name, Android request context, and provenance. The
response supplies the source ID used in the start-request JSON body.

Observed gRPC user agent:

```text
NotebookLM/1.46.7.940945420 (Android 16; sdk_gphone64_arm64)
```

### 2. Start the resumable upload

The app sends this HTTP/1.1 request to the project-scoped URL:

```http
POST /upload/upload/{project_id} HTTP/1.1
Authorization: Bearer [REDACTED]
Content-Type: text/plain; charset=utf-8
User-Agent: NotebookLM/1.46.7.940945420 (Android 16; sdk_gphone64_arm64)
X-Goog-AuthUser: 0
X-Goog-Upload-Command: start
X-Goog-Upload-Content-Length: 13362
X-Goog-Upload-File-Name: android-upload-smoke.pdf
X-Goog-Upload-Header-Content-Length: 13362
X-Goog-Upload-Header-Content-Type: application/pdf
X-Goog-Upload-Protocol: resumable
```

The body is proto3 JSON. IDs below are placeholders:

```json
{
  "projectId": "[PROJECT_ID]",
  "requestContext": {
    "clientType": "ANDROID_APP",
    "clientMetadata": {
      "clientVersion": "1.46.7.940945420"
    },
    "provenance": {
      "originProductType": "GOOGLE_NOTEBOOKLM",
      "clientInfo": {
        "applicationPlatform": "NATIVE",
        "device": "MOBILE_ANDROID",
        "applicationVersion": "1.46.7.940945420"
      }
    }
  },
  "sourceId": "[SOURCE_ID]",
  "provenance": {
    "originProductType": "GOOGLE_NOTEBOOKLM",
    "clientInfo": {
      "applicationPlatform": "NATIVE",
      "device": "MOBILE_ANDROID",
      "applicationVersion": "1.46.7.940945420"
    }
  }
}
```

The successful response was HTTP 200 with:

```text
X-Goog-Upload-Status: active
X-Goog-Upload-URL: https://notebooklm-pa.googleapis.com/upload/upload/{project_id}?upload_id=[REDACTED]&upload_protocol=resumable
```

### 3. Upload and finalize

The app performs an HTTP/1.1 `PUT` to the returned session URL:

```http
PUT /upload/upload/{project_id}?upload_id=[REDACTED]&upload_protocol=resumable HTTP/1.1
Authorization: Bearer [REDACTED]
User-Agent: Dart/3.13 (dart:io)
X-Goog-AuthUser: 0
X-Goog-Upload-Command: upload, finalize
X-Goog-Upload-Offset: 0

[raw file bytes]
```

The successful response was HTTP 200 with `X-Goog-Upload-Status: final`. The client then polls
`GetProject` until the new source is complete or failed.

## Artifact download sequence

### 1. List and select a ready representation

Call `ListArtifacts` over the mobile HTTP/2 gRPC service and select a ready artifact. The recovered
representation fields currently used by the reproducer are:

| representation | mobile response location |
|---|---|
| audio | artifact field 7 → media field 6 |
| video | artifact field 9 → media field 5 |
| infographic | artifact field 15 → item field 3 → image field 2 → URL field 1 |
| slide image | artifact field 17 → slide field 3 → image field 1 → URL field 1 |
| slide PDF | artifact field 17 → URL field 4 |
| slide PPTX | artifact field 17 → URL field 5 |
| file preview | artifact field 25 → URL field 3 |
| file download | artifact field 25 → URL field 4 |

The first live test selected a ready infographic. A later read-only test selected one ready slide
deck with both PDF and PPTX representations. Neither test printed an artifact ID, title, or URL.

### 2. Add the application-level redirect opt-in

Append this query parameter to the returned representation URL:

```text
alr=yes
```

The live infographic URL host was:

```text
lh3.googleusercontent.com
```

The live PDF and PPTX URLs both began on `contribution.usercontent.google.com`.

### APK control-flow confirmation

The pinned AOT library
`libNotebookLM_prod_android_library_flutter_artifacts.so` (SHA-256
`082d75e36eb03aea7ea5a8c252029c48b964177311ca4ebac6392814b8e6f81f`) contains the exact
download path. `ArtifactDownloadManager.download` receives the session `SSOHttpClient` and calls
`artifact_download_utils.downloadWithAlr(client, url, gcsHostsToStripAuth)`. That helper:

1. parses the representation URL and sets `alr=yes`;
2. constructs an ordinary HTTP `GET`;
3. sends through the authenticated `SSOHttpClient`; and
4. switches to a raw client only when the current host matches the configured storage-host list.

The multipart strings present elsewhere in the bundled `package:http` implementation are not
referenced by this path. No Drive form POST participates in the APK artifact-download control flow.

### 3. Authenticate the Googleusercontent request

For the live mobile URL, the OAuth bearer was required on the initial
`lh3.googleusercontent.com` request:

| request | result |
|---|---|
| `GET` with mobile bearer | HTTP 200 `image/png` |
| `GET` without bearer | HTTP 200 `text/plain`, then `lh3.google.com`, then Google sign-in, finally HTML |

The live slide control gave the same auth decision more directly:

| request to `contribution.usercontent.google.com` | result |
|---|---|
| `GET` with `alr=yes` and mobile bearer | HTTP 200 `application/octet-stream`, no redirect, for both PDF and PPTX |
| `GET` with `alr=yes` and no bearer | HTTP 302 `text/html` with a redirect |

This proves that the current mobile asset URL is not satisfied by an unauthenticated cookie-free
request. It also falsifies the planning assumption that the mobile bearer must never reach the
asset request. A mobile backend can either use this bearer-authenticated Googleusercontent path or
deliberately retain the existing cookie asset plane, but those are different implementations and
should not be conflated.

The reproducer follows both ordinary HTTP redirects and `text/plain` application redirects. It
keeps credentials only on a strict Google-host allowlist. If a redirect reaches a configured signed
GCS download host, it removes the bearer before fetching bytes. The live infographic returned image
bytes directly from `lh3.googleusercontent.com`, so the GCS branch was not exercised in this run.

### 4. Validate and publish bytes

The downloader:

1. writes to an owner-private temporary file beside the destination;
2. hashes bytes while streaming;
3. rejects empty responses;
4. checks the expected media signature; and
5. atomically replaces the destination only after success.

The live infographic result was:

```text
media type: image/png
dimensions: 1536 × 2752
bytes: 4,721,650
sha256: 7eaaaec02d881f67ffbdfb5417b0bee35d10c9b458b98df64e9ff06c590f3536
```

The same public Android API was then exercised for both slide formats through
`NotebookLMClient.from_storage(..., backend="android")`. The PDF passed its `%PDF-` signature
check. The PPTX passed ZIP validation and contained the required OOXML content-types file and
presentation directory. Both responses declared `application/octet-stream`, so the downloader
admits that live MIME only for slide representations and still requires the format-specific byte
signature before atomic publication. Both temporary outputs were deleted after validation; this
read-only probe created no notebook or external Drive resource.

## Reproducer usage

The authorized master-token profile was used directly; its account metadata and all IDs remain
local.

List ready artifacts and their available representations:

```bash
uv run scripts/reproduce_android_transfer.py list-artifacts \
  --profile PROFILE
```

Upload a file, defaulting to the profile's `multi_source_notebook_id`:

```bash
uv run scripts/reproduce_android_transfer.py upload ./document.pdf \
  --profile PROFILE
```

Download the first ready representation, defaulting to the profile's
`generation_notebook_id`:

```bash
uv run scripts/reproduce_android_transfer.py download ./artifact.bin \
  --profile PROFILE \
  --representation auto
```

Use `--notebook-id` and `--artifact-id` for explicit selection. The script refuses non-HTTPS or
unapproved upload/download hosts, does not follow redirects automatically, and does not overwrite a
destination unless `--overwrite` is passed.

## Detailed interception instructions

The official APK bundle can be intercepted without patching or re-signing it. The working path is:

```text
Official NotebookLM APK
  → HTTP Toolkit Android companion VPN
  → HTTP Toolkit CA injected into the Android Conscrypt APEX namespace
  → UID-scoped DNAT to a redacting Mockttp recorder
  → real Google upstream
```

The general gRPC setup is also documented in [`capture.md`](capture.md). The steps below add Scotty
upload and `lh3` artifact-transfer capture with stricter redaction.

### 1. Boot the rootable emulator

Use a Google APIs image, not a production-locked Google Play image:

```bash
/opt/homebrew/share/android-commandlinetools/emulator/emulator \
  @notebooklm361 \
  -writable-system \
  -no-snapshot \
  -no-boot-anim \
  -no-audio \
  -gpu host
```

Wait for boot and enable ADB root:

```bash
adb wait-for-device
until test "$(adb shell getprop sys.boot_completed | tr -d '\r')" = "1"; do
  sleep 1
done
adb root
adb shell id
```

The verified image is Android 16/API 36.1 ARM64. `adb shell id` must report root before attempting
system certificate injection.

### 2. Install and launch the original split APK

```bash
notebooklm_apk_bundle=/path/to/notebooklm.apk

adb install-multiple -r \
  "$notebooklm_apk_bundle/base.apk" \
  "$notebooklm_apk_bundle/split_config.arm64_v8a.apk" \
  "$notebooklm_apk_bundle/split_config.xxhdpi.apk" \
  "$notebooklm_apk_bundle/split_config.xxxhdpi.apk"

adb shell am start \
  -n com.google.android.apps.labs.language.tailwind/.MainActivityDev
```

Sign in and verify that the notebook list loads **before** interception. Do not type a Google
password or refresh account credentials while capture is active.

### 3. Connect HTTP Toolkit through its ADB interceptor

Start HTTP Toolkit on macOS, choose the Android/ADB interceptor for `emulator-5554`, and wait for
the Android companion to display all three conditions:

```text
Connected
USER TRUST ENABLED
SYSTEM TRUST ENABLED
```

The companion may display either `10.0.2.2:8000` or a reachable LAN address such as
`192.168.x.x:8000`. Use the address it actually configured; do not hard-code one from an older run.

On Android 14+, a legacy `/system/etc/security/cacerts` installation is insufficient. Verify that
the live NotebookLM process sees the HTTP Toolkit CA in the Conscrypt APEX namespace:

```bash
notebooklm_ca_hash="$(
  openssl x509 -subject_hash_old -noout \
    -in "$HOME/Library/Preferences/httptoolkit/ca.pem"
)"
notebooklm_pid="$(
  adb shell pidof com.google.android.apps.labs.language.tailwind | tr -d '\r'
)"

test "${#notebooklm_ca_hash}" -eq 8
test -n "$notebooklm_pid"
adb shell su 0 nsenter -t "$notebooklm_pid" -m -- \
  ls -l "/apex/com.android.conscrypt/cacerts/$notebooklm_ca_hash.0"
```

The injected mount is runtime-only. Re-select the HTTP Toolkit ADB interceptor after every emulator
reboot, even if the companion remains installed.

### 4. Install and start the redacting transfer recorder

The original recorder, `scripts/capture_mobile_transfer.js`, lived in the same
separate mobile-evidence workspace and is not part of this repository.
It uses Mockttp directly, so a paid HTTP Toolkit HAR export is neither required nor bypassed.

Install the verified Mockttp dependency outside either repository:

```bash
npm install --prefix /tmp/notebooklm-mockttp mockttp@4.5.0
```

Resolve fresh public upstream addresses through DNS-over-HTTPS. This avoids synthetic DNS addresses
returned by some host VPNs:

```bash
notebooklm_prod_ip="$(
  curl -fsS \
    'https://dns.google/resolve?name=notebooklm-pa.googleapis.com&type=A' \
    | jq -r '.Answer[] | select(.type == 1) | .data' \
    | head -1
)"
notebooklm_autopush_ip="$(
  curl -fsS \
    'https://dns.google/resolve?name=autopush-notebooklm-pa.sandbox.googleapis.com&type=A' \
    | jq -r '.Answer[] | select(.type == 1) | .data' \
    | head -1
)"
notebooklm_lh3usercontent_ip="$(
  curl -fsS \
    'https://dns.google/resolve?name=lh3.googleusercontent.com&type=A' \
    | jq -r '.Answer[] | select(.type == 1) | .data' \
    | head -1
)"
notebooklm_lh3google_ip="$(
  curl -fsS \
    'https://dns.google/resolve?name=lh3.google.com&type=A' \
    | jq -r '.Answer[] | select(.type == 1) | .data' \
    | head -1
)"

test -n "$notebooklm_prod_ip"
test -n "$notebooklm_autopush_ip"
test -n "$notebooklm_lh3usercontent_ip"
test -n "$notebooklm_lh3google_ip"
```

Start the recorder from `gemini-notebook-mobile` and leave it running:

```bash
cd /path/to/gemini-notebook-mobile

NOTEBOOKLM_PRODUCTION_IP="$notebooklm_prod_ip" \
NOTEBOOKLM_AUTOPUSH_IP="$notebooklm_autopush_ip" \
NOTEBOOKLM_LH3_GOOGLEUSERCONTENT_IP="$notebooklm_lh3usercontent_ip" \
NOTEBOOKLM_LH3_GOOGLE_IP="$notebooklm_lh3google_ip" \
NOTEBOOKLM_CAPTURE_FILE=/tmp/notebooklm-mobile-transfer.jsonl \
node scripts/capture_mobile_transfer.js
```

The output directory/file is owner-only. The recorder does not save authorization values, project
IDs, source IDs, artifact paths, upload-session values, file names, or uploaded/downloaded bodies.
For download requests it records only whether authorization was present.

### 5. Divert new companion connections to the recorder

**The companion must already be connected to HTTP Toolkit on port 8000.** If this rule is installed
first, it also captures the companion's control connection and activation fails.

Resolve the companion UID and its currently configured proxy host:

```bash
notebooklm_vpn_uid="$(
  adb shell pm list packages -U tech.httptoolkit.android.v1 \
    | sed -n 's/.* uid:\([0-9][0-9]*\).*/\1/p' \
    | tr -d '\r'
)"
notebooklm_proxy_host="$(
  adb shell dumpsys connectivity \
    | sed -n 's/.*VPN:tech\.httptoolkit\.android\.v1.*HttpProxy: \[\([^]]*\)\] 8000.*/\1/p' \
    | head -1 \
    | tr -d '\r'
)"

test -n "$notebooklm_vpn_uid"
test "$notebooklm_vpn_uid" -ge 10000
case "$notebooklm_proxy_host" in
  ''|*[!0-9.]*) echo "invalid HTTP Toolkit proxy host" >&2; exit 1 ;;
esac
```

Add an owner-scoped DNAT rule. It changes only connections opened by the HTTP Toolkit companion:

```bash
adb shell su 0 iptables -t nat -C OUTPUT \
  -p tcp -d "$notebooklm_proxy_host" --dport 8000 \
  -m owner --uid-owner "$notebooklm_vpn_uid" \
  -j DNAT --to-destination "$notebooklm_proxy_host:8081" \
  2>/dev/null || \
adb shell su 0 iptables -t nat -A OUTPUT \
  -p tcp -d "$notebooklm_proxy_host" --dport 8000 \
  -m owner --uid-owner "$notebooklm_vpn_uid" \
  -j DNAT --to-destination "$notebooklm_proxy_host:8081"
```

Force a **new NotebookLM connection** after adding the rule; pre-existing connections continue to
port 8000:

```bash
adb shell am force-stop com.google.android.apps.labs.language.tailwind
adb shell am start \
  -n com.google.android.apps.labs.language.tailwind/.MainActivityDev
```

Verify that the DNAT packet counter increases:

```bash
adb shell su 0 iptables -t nat -L OUTPUT -n -v --line-numbers \
  | grep '8000.*8081'
```

`ss` may continue to display the original destination port; the DNAT counter and recorder events
are the reliable checks.

### 6. Exercise a disposable upload

Use a new throwaway notebook and a small non-private fixture. In the app:

1. tap **Create New**;
2. choose **PDF** under **Or upload your files**;
3. select the fixture in Android's file picker; and
4. wait until the source appears and finishes processing.

Expected redacted event sequence:

```text
request  AddTentativeSources                         HTTP/2
response AddTentativeSources                         200
request  /upload/upload/[UUID]                       HTTP/1.1 start
response /upload/upload/[UUID]                       200 active
request  /upload/upload/[UUID]?upload_id=[REDACTED]  HTTP/1.1 PUT/finalize
response /upload/upload/[UUID]?upload_id=[REDACTED]  200 final
```

Delete the throwaway notebook after the capture.

### 7. Exercise an artifact download

Open a notebook that already has a ready artifact, select **Studio**, open the artifact, and use its
**Download** action. An infographic is convenient because its signature is easy to validate.

The recorder recognizes `lh3.googleusercontent.com` and `lh3.google.com` GETs. It stores the host,
HTTP version, status, media type, response size, redacted redirect shape, and
`authorization_present`; it never stores the URL path, query values, bearer, or image/media body.

Not every representation takes the same route. The verified infographic returned PNG bytes directly
from `lh3.googleusercontent.com` when authorized. Other artifacts may return a small `text/plain`
application redirect or an ordinary HTTP redirect; the recorder records only the target host, path
segment count, extension, and query-key names.

### 8. Inspect only sanitized records

```bash
jq -c '{
  direction,
  kind,
  host,
  path,
  method,
  http_version,
  status_code,
  headers,
  authorization_present,
  session_url,
  redirect,
  application_redirect,
  body
}' /tmp/notebooklm-mobile-transfer.jsonl
```

Do not export or share HTTP Toolkit's raw HAR. Its in-memory/raw request view contains the OAuth
bearer and may contain private notebook data.

### 9. Remove the diversion before stopping the recorder

```bash
adb shell su 0 iptables -t nat -D OUTPUT \
  -p tcp -d "$notebooklm_proxy_host" --dport 8000 \
  -m owner --uid-owner "$notebooklm_vpn_uid" \
  -j DNAT --to-destination "$notebooklm_proxy_host:8081"

adb shell su 0 iptables -t nat -S OUTPUT | grep '8000\|8081'
```

The final command should print nothing. Now stop the recorder with `Ctrl-C`, restart NotebookLM if
continued app use is needed, and press **Disconnect** in the HTTP Toolkit Android companion.

Delete the owner-private capture after extracting sanitized protocol facts:

```bash
unlink /tmp/notebooklm-mobile-transfer.jsonl
```

### Troubleshooting

- **Companion says it cannot connect:** remove the DNAT rule, reactivate the ADB interceptor, wait
  for `Connected`, then reinstall the rule.
- **App fails TLS after reboot:** re-run the ADB interceptor and verify the CA inside the app's
  Conscrypt APEX mount namespace.
- **Recorder is silent but the app works:** force-stop and restart NotebookLM so it opens new proxy
  connections; existing sockets bypass a newly added DNAT rule.
- **Recorder receives traffic but upstream TLS stalls:** refresh the DNS-over-HTTPS IPs; do not
  reuse a synthetic host-VPN DNS answer.
- **Login breaks:** remove the DNAT rule and disconnect interception. Authenticate before capture,
  never during it.

The live run removed the UID-scoped rule, disconnected the companion VPN, deleted temporary raw
captures, and deleted the disposable notebook after validation.

## What did not work

- Guessing the upload endpoint from the base URL alone produced repeatable HTTP 404 responses.
- Appending the source ID looked plausible but still produced HTTP 404.
- Autopush rejected the consumer account before upload handling.
- Merely installing a CA under the legacy Android system path was insufficient on Android 16; the
  app process needed the HTTP Toolkit CA in its Conscrypt APEX mount namespace.
- An unauthenticated request to the live `lh3.googleusercontent.com` representation URL did not
  return artifact bytes.

The successful app capture, ID-role comparison, corrected replay, and byte-level download
validation close each of those gaps.
