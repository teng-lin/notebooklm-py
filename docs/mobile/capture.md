# Capturing the NotebookLM Android gRPC API

**Status:** Experimental, working locally

**Last Verified:** 2026-07-21

**Scope:** Traffic discovery against the official NotebookLM Android app, without APK
patching or re-signing. Mostly read-only, but [Exercising write paths](#exercising-write-paths-throwaway-notebook-only)
deliberately drives create / delete / share / chat RPCs — **always against a disposable
notebook**, never real data.

This runbook records the setup that successfully captured and split NotebookLM Android's
HTTP/2 gRPC traffic into raw protobuf messages. It also records failed approaches so the
investigation does not repeat them.

The current recorder is [`scripts/capture_mobile_grpc.js`](../../scripts/capture_mobile_grpc.js).

## Result

The working path is:

```text
Official NotebookLM APK
        |
        | Android socket traffic
        v
HTTP Toolkit Android VPN companion
        |
        | TCP to emulator host 10.0.2.2:8000
        | UID-scoped DNAT changes only the destination port
        v
Mockttp recorder on macOS 10.0.2.2:8081
        |
        | HTTP/2 + TLS, real Google IP pinned for upstream
        v
notebooklm-pa.googleapis.com:443
```

This captures requests under:

```text
/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/<Method>
```

The transport properties observed so far are:

- HTTP/2 `POST`
- `content-type: application/grpc`
- standard gRPC trailers, including `grpc-status: 0` on successful calls
- one or more length-prefixed protobuf messages in each request or response body
- OAuth bearer authentication added by the app; the recorder deliberately does not save headers

## Verified environment

| Component | Verified value |
|---|---|
| Host | macOS on Apple Silicon |
| Android virtual device | `notebooklm361` |
| Android image | Android 16 / API 36.1, `google_apis`, ARM64 |
| AVD product | `sdk_gphone64_arm64` |
| Root access | `adb shell` runs as `uid=0` |
| NotebookLM package | `com.google.android.apps.labs.language.tailwind` |
| Main activity | `.MainActivityDev` |
| NotebookLM version | `1.46.7.940945420` (`versionCode=138238`) |
| Target SDK | 37 |
| HTTP Toolkit | 1.26.1 |
| HTTP Toolkit companion | `tech.httptoolkit.android.v1` |
| Mockttp | 4.5.0, installed under `/tmp` |
| Node.js | 26.4.0 |
| ADB | 1.0.41 / platform tools 37.0.0 |

The APK bundle is split across these files:

```text
notebooklm.apk/base.apk
notebooklm.apk/split_config.arm64_v8a.apk
notebooklm.apk/split_config.xxhdpi.apk
notebooklm.apk/split_config.xxxhdpi.apk
```

Install all splits together. Installing only `base.apk` is incomplete.

## Security rules

Treat this environment and every capture as account-sensitive.

1. Use a disposable rooted emulator, not a personal phone.
2. Complete Google sign-in before enabling interception.
3. Do not type a password while a capture is active.
4. Do not share HTTP Toolkit screenshots or raw HAR files. Request details contain an OAuth
   bearer token.
5. Do not commit `.pb`, `.raw`, `.mitm`, HAR, or `index.jsonl` capture files. Protobuf bodies can
   contain notebook IDs, source text, chat history, and other private data.
6. Keep the official APK signature. Patching and re-signing are neither required nor desirable
   for this workflow.
7. Rotate any credential that is accidentally printed or captured outside the local machine.

The recorder writes only method metadata and raw protobuf bodies. It does not persist request or
response headers. Its output directory and files use owner-only permissions.

## Step-by-step setup

### 1. Boot a rootable modern emulator

Use a **Google APIs** image, not a Google Play image. The latter is normally production-locked and
not suitable for system certificate injection.

The working AVD has this system image:

```text
system-images/android-36.1/google_apis/arm64-v8a/
```

Create it in Android Studio's Device Manager if `notebooklm361` does not already exist. Boot it
without snapshots and with a writable system:

```bash
/opt/homebrew/share/android-commandlinetools/emulator/emulator \
  @notebooklm361 \
  -writable-system \
  -no-snapshot \
  -no-boot-anim \
  -no-audio \
  -gpu host
```

In another terminal, wait for boot and verify root:

```bash
adb wait-for-device
until test "$(adb shell getprop sys.boot_completed | tr -d '\r')" = "1"; do
  sleep 1
done
adb root
adb shell id
```

Expected identity:

```text
uid=0(root) gid=0(root) ... context=u:r:su:s0
```

### 2. Install the original split APKs

From the repository root:

```bash
adb install-multiple -r \
  notebooklm.apk/base.apk \
  notebooklm.apk/split_config.arm64_v8a.apk \
  notebooklm.apk/split_config.xxhdpi.apk \
  notebooklm.apk/split_config.xxxhdpi.apk
```

Verify the package and launcher activity:

```bash
adb shell dumpsys package com.google.android.apps.labs.language.tailwind \
  | rg 'versionName=|versionCode=|userId='
adb shell cmd package resolve-activity \
  --brief \
  -c android.intent.category.LAUNCHER \
  com.google.android.apps.labs.language.tailwind
```

The launcher should resolve to:

```text
com.google.android.apps.labs.language.tailwind/.MainActivityDev
```

### 3. Sign in before interception

Clear any stale global proxy settings. Flutter's gRPC stack does not use these settings for the
API calls, but stale values can break Google sign-in and other Google services.

```bash
adb shell settings delete global http_proxy
adb shell settings delete global global_http_proxy_host
adb shell settings delete global global_http_proxy_port
adb shell settings delete global global_http_proxy_exclusion_list
adb shell settings delete global global_proxy_pac_url
```

Launch the app directly:

```bash
adb shell am start \
  -n com.google.android.apps.labs.language.tailwind/.MainActivityDev
```

Finish Google sign-in and confirm that the notebook list loads. Do not enable interception until
this succeeds. Clearing app data after this point removes the login and should be avoided.

### 4. Install HTTP Toolkit and connect the ADB device

Install and launch HTTP Toolkit:

```bash
brew install --cask http-toolkit
open -a "HTTP Toolkit"
```

In HTTP Toolkit, select the Android device/ADB interceptor for `emulator-5554`. HTTP Toolkit:

1. installs `tech.httptoolkit.android.v1` on the emulator;
2. creates an Android VPN that forwards app sockets to the host proxy;
3. installs its CA as both a user and system CA; and
4. injects the CA into the Android 14+ Conscrypt APEX certificate store and existing app mount
   namespaces.

The companion screen must show both:

```text
USER TRUST ENABLED
SYSTEM TRUST ENABLED
```

It should also show a connection to:

```text
10.0.2.2 on port 8000
```

Confirm the companion and the absence of a global Android proxy:

```bash
adb shell pm list packages -U tech.httptoolkit.android.v1
adb shell settings get global http_proxy
```

The second command should print `null`. Routing is provided by the companion VPN, not Android's
global HTTP proxy.

#### Why the APEX injection matters

On recent Android images, adding a certificate only to `/system/etc/security/cacerts` is not
enough. Conscrypt is delivered as an APEX module, and apps see certificates under:

```text
/apex/com.android.conscrypt/cacerts
```

HTTP Toolkit's rooted-device setup mounts a certificate directory into the zygote namespace and
propagates it to existing child app namespaces. Verify that the NotebookLM process sees the HTTP
Toolkit CA:

```bash
http_toolkit_hash="$({
  openssl x509 -subject_hash_old -noout \
    -in "$HOME/Library/Preferences/httptoolkit/ca.pem"
} | tr -d '\r')"
test "${#http_toolkit_hash}" -eq 8
notebooklm_pid="$(adb shell pidof com.google.android.apps.labs.language.tailwind | tr -d '\r')"
test -n "$notebooklm_pid"
adb shell "nsenter -t $notebooklm_pid -m -- ls -l \
  /apex/com.android.conscrypt/cacerts/$http_toolkit_hash.0"
```

The mount is runtime-only and disappears when the emulator reboots. Reconnect the ADB interceptor
after each reboot.

### 5. Resolve a real Google upstream IP

The host VPN used during this investigation returns synthetic `198.20.0.x` DNS addresses and
routes them through a `utun` interface. That path works for HTTP Toolkit itself but broke the
standalone proxy's upstream HTTP/2 handshake.

Resolve a public address through DNS-over-HTTPS instead:

```bash
notebooklm_upstream_ip="$({
  curl -fsS \
    'https://dns.google/resolve?name=notebooklm-pa.googleapis.com&type=A' \
    | jq -er '.Answer[] | select(.type == 1) | .data'
} | head -1)"
test -n "$notebooklm_upstream_ip"
printf 'NotebookLM upstream: %s\n' "$notebooklm_upstream_ip"
```

Validate TLS and HTTP/2 while preserving the hostname:

```bash
curl --http2 \
  --resolve "notebooklm-pa.googleapis.com:443:$notebooklm_upstream_ip" \
  -sS \
  -o /dev/null \
  -w '%{http_code} %{remote_ip} HTTP/%{http_version}\n' \
  https://notebooklm-pa.googleapis.com/
```

The root endpoint returned `404` over HTTP/2 in the verified setup. The `404` is expected; it
proves that TLS, SNI, routing, and HTTP/2 reached the Google frontend.

Resolve again when starting a new capture session. Do not permanently hard-code the observed IP.

### 6. Install Mockttp outside the project

HTTP Toolkit's UI can inspect the traffic, but raw export is a Pro feature. Do not bypass that
gate. The recorder uses the same open-source Mockttp engine directly and stores only the required
gRPC frames.

Install the verified Mockttp version under `/tmp`:

```bash
npm install --prefix /tmp/notebooklm-mockttp mockttp@4.5.0
```

This does not add a dependency to `notebooklm-py`.

### 7. Start the redacted gRPC recorder

Keep the shell variable from step 5 and start the recorder:

```bash
NOTEBOOKLM_UPSTREAM_IP="$notebooklm_upstream_ip" \
NOTEBOOKLM_CAPTURE_DIR=/tmp/notebooklm-mobile-grpc \
node scripts/capture_mobile_grpc.js
```

Expected startup output:

```text
Capturing notebooklm-pa.googleapis.com gRPC on :8081; output: /tmp/notebooklm-mobile-grpc
Captured protobuf bodies can contain private notebook data. Do not commit them.
```

The recorder:

- listens on host port 8081;
- uses HTTP Toolkit's already-trusted CA and private key;
- accepts the VPN companion's intercepted HTTP/2 traffic;
- forwards NotebookLM requests to the real IP from step 5 while preserving the original
  HTTP/2 `:authority` value;
- passes unmatched traffic through without recording it;
- omits all headers, including `authorization`;
- removes the five-byte gRPC envelope;
- decompresses gzip-compressed gRPC messages; and
- writes each protobuf message as an owner-readable `.pb` file.

### 8. Redirect only the VPN companion to the recorder

HTTP Toolkit's companion currently targets host port 8000. Redirect new connections from that
companion UID to port 8081:

```bash
notebooklm_vpn_uid="$({
  adb shell pm list packages -U tech.httptoolkit.android.v1 \
    | sed -n 's/.* uid:\([0-9][0-9]*\).*/\1/p'
} | tr -d '\r')"
test -n "$notebooklm_vpn_uid"
test "$notebooklm_vpn_uid" -ge 10000
printf 'HTTP Toolkit companion UID: %s\n' "$notebooklm_vpn_uid"

adb shell "iptables -t nat -C OUTPUT \
  -p tcp -d 10.0.2.2 --dport 8000 \
  -m owner --uid-owner $notebooklm_vpn_uid \
  -j DNAT --to-destination 10.0.2.2:8081 \
  2>/dev/null || \
iptables -t nat -A OUTPUT \
  -p tcp -d 10.0.2.2 --dport 8000 \
  -m owner --uid-owner $notebooklm_vpn_uid \
  -j DNAT --to-destination 10.0.2.2:8081"
```

Verify the exact rule:

```bash
adb shell iptables -t nat -S OUTPUT | rg '8000|8081'
```

Expected shape:

```text
-A OUTPUT -d 10.0.2.2/32 -p tcp ... --dport 8000 ... --uid-owner <uid> \
  -j DNAT --to-destination 10.0.2.2:8081
```

This rule redirects the VPN companion, not the NotebookLM app UID. The companion keeps ownership
of VPN routing; Mockttp replaces HTTP Toolkit's host proxy as the traffic decoder and recorder.

#### If the `10.0.2.2:8081` diversion never establishes

On some hosts (notably when a host-side VPN hands the emulator synthetic `198.20.x.x` DNS, per
[Synthetic VPN DNS addresses](#synthetic-vpn-dns-addresses)), a kernel DNAT whose **destination
is `10.0.2.2`** fails its return path through QEMU's user-net: the companion's SYNs are rewritten
(the rule's packet counter climbs) but no connection completes and the recorder logs nothing. A
direct `nc 10.0.2.2 8081` from the device still works, which distinguishes this from a dead
recorder.

Route through the loopback + an `adb reverse` tunnel instead — this path was verified on
2026-07-22:

> **Remove the primary rule first.** The `10.0.2.2` DNAT added above is appended to the same
> `OUTPUT` chain and also matches `--dport 8000`, so it wins on ordering and this fallback would
> never fire while it is still installed.

```bash
# 1. drop the primary 10.0.2.2 DNAT so it cannot shadow the loopback rule
adb shell "iptables -t nat -D OUTPUT \
  -p tcp -d 10.0.2.2 --dport 8000 \
  -m owner --uid-owner $notebooklm_vpn_uid \
  -j DNAT --to-destination 10.0.2.2:8081" 2>/dev/null || true

# 2. record the prior value so cleanup can restore it
prior_route_localnet=$(adb shell su 0 cat /proc/sys/net/ipv4/conf/all/route_localnet | tr -d '\r')

adb shell su 0 sh -c 'echo 1 > /proc/sys/net/ipv4/conf/all/route_localnet'
adb reverse tcp:8081 tcp:8081          # device 127.0.0.1:8081 -> host 8081 (Mockttp)
# divert the companion to device loopback rather than 10.0.2.2:
adb shell su 0 iptables -t nat -A OUTPUT -p tcp --dport 8000 \
  -m owner --uid-owner "$notebooklm_vpn_uid" \
  -j DNAT --to-destination 127.0.0.1:8081
```

`route_localnet=1` lets the DNAT'd packet reach the on-device `adb reverse` listener, which
tunnels reliably to the host recorder.

### 9. Force a new NotebookLM connection

Existing sockets continue using port 8000, so restart only NotebookLM after adding the rule:

```bash
adb shell am force-stop com.google.android.apps.labs.language.tailwind
adb shell am start \
  -n com.google.android.apps.labs.language.tailwind/.MainActivityDev
```

The login persists. A successful bootstrap prints records similar to:

```text
001 request GetOrCreateAccount 91
001 response GetOrCreateAccount 41
```

## Exercising read-only UI paths

Perform one UI action at a time and wait for the matching response before moving on. The following
paths were verified.

| UI action | RPCs observed | Request bytes | Response bytes | Result |
|---|---|---:|---:|---|
| Launch app to notebook list | `GetOrCreateAccount` | 91 | 41 | HTTP 200, gRPC 0 |
| Open an existing notebook | `GetProject` | 105 | 21,885–21,886 | HTTP 200, gRPC 0 |
| Open an existing notebook | `GenerateNotebookGuide` | 101 | 1,851 | HTTP 200, gRPC 0 |
| Enter notebook chat | `ListChatSessions` | 129 | 40 | HTTP 200, gRPC 0 |
| Enter notebook chat | `ListChatTurns` | 101 | 991,051 | HTTP 200, gRPC 0 |
| Select the Sources tab | no new RPC | — | — | Data was already present in `GetProject` |
| Select the Studio tab | `ListArtifacts` | 101 | 64,223 | HTTP 200, gRPC 0 |
| Select the Studio tab | `GetNotes` | 101 | 4,460 | HTTP 200, gRPC 0 |

`GenerateNotebookGuide` was emitted automatically while opening the notebook even though the
operator only navigated through read-only screens. Treat it as potentially stateful until its
request and response semantics are decoded; do not replay it yet.

The large `ListChatTurns` response confirms why capture outputs must remain private.

### Exercising write paths (throwaway notebook only)

Mutations were captured on 2026-07-22 by driving a **disposable** notebook to avoid touching real
data. Perform each action, wait for its response, then move on:

| UI action | RPCs observed |
|---|---|
| Create notebook + add a Website source | `CreateProject` → `AddTentativeSources` → `AddSources` |
| Studio → Audio Overview | `CreateArtifact`, then repeated `GetArtifact` polling |
| Open a source | `LoadSource` (full text), `GenerateDocumentGuides` |
| Rename notebook | `MutateProject` |
| Delete a source | `DeleteSources` |
| Delete notebook | `DeleteProjects` |
| "Find sources from the web" (research) | `DiscoverSources`, then `AddTentativeSources`/`AddSources` on import |
| Share → Manage notebook access → Save | `GetProjectDetails`, `ShareProject` (`LabsTailwindSharingService`) |
| Chat → ask a question | `GenerateFreeFormStreamed` (**server-streaming**) |

Always use a throwaway notebook and delete it afterward; these RPCs create, mutate, and delete
account data.

`GenerateFreeFormStreamed` is the only server-streaming call observed: the recorder writes one
`.pb` per response frame (30+ frames in one sample) and each frame is a cumulative snapshot of
the growing answer, so the last frame holds the complete reply. Send the message with the blue
send button — the on-screen keyboard's Enter key inserts a newline instead of sending.

## Capture output

The output directory contains one index and one file per gRPC message:

```text
/tmp/notebooklm-mobile-grpc/
├── index.jsonl
├── 001_GetOrCreateAccount.request.0.pb
├── 001_GetOrCreateAccount.response.0.pb
├── 002_GetProject.request.0.pb
└── 002_GetProject.response.0.pb
```

Each `index.jsonl` record contains only:

- capture ID;
- method and full RPC path for requests;
- direction;
- HTTP version or response status;
- gRPC status;
- protobuf filenames and sizes; and
- a framing error plus a `.raw` filename when a body is not valid standard gRPC framing.

Inspect the safe metadata without printing message bodies:

```bash
jq -c \
  '{id,method,direction,http_version,status_code,grpc_status,frames,frame_error}' \
  /tmp/notebooklm-mobile-grpc/index.jsonl
```

A gRPC message is framed as:

```text
byte 0      compression flag (0 or 1)
bytes 1–4   unsigned big-endian protobuf message length
bytes 5…    protobuf message
```

The recorder removes this envelope. Each `.pb` file is the protobuf message itself.

Schema recovery is done for the observed methods. Decode captured `.pb` bodies into merged,
redacted schemas with [`scripts/decode_mobile_grpc.py`](../../scripts/decode_mobile_grpc.py):

```bash
python scripts/decode_mobile_grpc.py /tmp/notebooklm-mobile-grpc <MethodSubstring>
```

The recovered endpoint and message shapes — including create/rename/delete, source add/remove,
artifact generation, web-source discovery, chat, and sharing — are written up in
[docs/mobile/endpoints.md](endpoints.md). Field numbers and wire types there are
ground truth; semantic names are inferred. Do not assign semantic field names from a single sample.

### Enumerating the full method surface without traffic

Capture only reveals RPCs the UI actually calls. To enumerate the **complete** API surface —
including methods compiled into the app but not wired to any screen — pull the method-path
strings straight out of the Flutter AOT library:

```bash
unzip -o notebooklm.apk/split_config.arm64_v8a.apk 'lib/*' -d /tmp/nblm-apk
strings -a /tmp/nblm-apk/lib/arm64-v8a/libNotebookLM_prod_android_library_flutter_artifacts.so \
  | grep -oE '/[a-z][a-z0-9_.]+\.[A-Z][A-Za-z0-9]+/[A-Z][A-Za-z0-9]+' | sort -u
```

This yields 49 methods across 4 gRPC services. Cross-referencing them against the web
`rpc/types.py` rpcids shows, for example, that label mutation and deep-research job RPCs exist
on the server but are **not** compiled into the mobile app. See the cross-reference table in
[docs/mobile/endpoints.md](endpoints.md#mobile--web-cross-reference).

The **full protobuf schema** (message/field names, tags, types) was recovered by decompiling the
Flutter AOT snapshot with [blutter](https://github.com/worawit/blutter) ported to Dart 3.13 (the
app's `3.13.0-256.0.dev` build). The port is saved as
[docs/mobile/blutter-dart3.13.patch](blutter-dart3.13.patch); [scripts/parse_pbschema.py](../../scripts/parse_pbschema.py)
turns blutter's disassembled `BuilderInfo._i()` methods into
[docs/mobile/schema.proto](schema.proto) (282 messages, 767 fields). See
[docs/mobile/endpoints.md](endpoints.md#recovering-the-remaining-field-names-reversing-the-binary)
for the exact Dart-3.13 changes and build steps.

## Stopping and restoring HTTP Toolkit

Remove the DNAT rule before stopping the recorder so the VPN never points at a closed port.
**Run whichever block matches the path you used** — the loopback fallback leaves three pieces of
state behind, not one, and a leftover DNAT rule or a device left at `route_localnet=1` will
silently misroute later sessions.

Primary path (`10.0.2.2` DNAT):

```bash
adb shell "iptables -t nat -D OUTPUT \
  -p tcp -d 10.0.2.2 --dport 8000 \
  -m owner --uid-owner $notebooklm_vpn_uid \
  -j DNAT --to-destination 10.0.2.2:8081"
```

Loopback fallback path — remove the rule, the tunnel, **and** restore `route_localnet`:

```bash
adb shell su 0 iptables -t nat -D OUTPUT -p tcp --dport 8000 \
  -m owner --uid-owner "$notebooklm_vpn_uid" \
  -j DNAT --to-destination 127.0.0.1:8081

adb reverse --remove tcp:8081

# restore the value captured before the fallback was installed (default is 0)
adb shell su 0 sh -c "echo ${prior_route_localnet:-0} > /proc/sys/net/ipv4/conf/all/route_localnet"
```

Verify nothing is left behind:

```bash
adb shell su 0 iptables -t nat -S OUTPUT | grep 8000   # expect no output
adb reverse --list                                      # expect no 8081 entry
```

Press `Ctrl-C` in the recorder terminal. Then restart NotebookLM to open a fresh connection to
HTTP Toolkit's original port 8000:

```bash
adb shell am force-stop com.google.android.apps.labs.language.tailwind
adb shell am start \
  -n com.google.android.apps.labs.language.tailwind/.MainActivityDev
```

When interception is no longer needed, press **Disconnect** in the HTTP Toolkit Android companion.
The injected APEX certificate mount disappears on emulator reboot.

## What did not work

### Patching or re-signing the APK

Not attempted as part of the working path. It is unnecessary and risks breaking Google's package
identity and sign-in checks. The official split APK runs unchanged.

### Older Android images

- API 30 had Google Play services too old for the current app/login flow.
- API 33 still rejected the app identity during Google sign-in.
- API 36.1 with current Google APIs and the official Google-signed APK completed sign-in.

### Android's global HTTP proxy

The global proxy captured some Google/system traffic, but NotebookLM's Flutter gRPC client opened
direct sockets and ignored it. A VPN/TUN interceptor is required.

### Frida to trust the CA / bypass pinning

The HTTP Toolkit `frida-scripts/` kit was tried as a GUI-free alternative to the desktop
interceptor. `frida-server` (run as root) attaches fine and the app runs normally under a
**bare** frida session, but loading `native-tls-hook.js` (which hooks statically-linked BoringSSL
in Flutter) reliably **crashes this app** at script-load time — as does the full unpinning chain.
The `native-connect-hook.js` "redirect all TCP" also destabilises startup. Frida was abandoned;
HTTP Toolkit's own CA injection is what makes the Flutter app trust interception.

### Hand-rolled APEX cert injection via `nsenter` mounts

Bind-mounting a custom cacerts overlay into the Conscrypt APEX and propagating it with
`nsenter --mount=/proc/<pid>/ns/mnt -- mount --bind …` **hangs** on this image: the global mount
succeeds instantly, but per-namespace `nsenter` mount calls block, and repeating them wedged
`adbd` until the emulator went offline and needed a reboot. Let HTTP Toolkit's ADB interceptor do
the cert injection; do not script the namespace mounts by hand.

### Direct app-UID DNAT to mitmproxy reverse mode

Routing and certificate trust worked, but mitmproxy 12.2.3 rejected the upstream transition with:

```text
Initiating HTTP/2 connections with prior knowledge are currently not supported.
```

Using the HTTP Toolkit VPN to feed mitmproxy's regular listener reached the same failure. Replacing
mitmproxy with Mockttp fixed it because Mockttp supports this HTTP/2 path.

### Synthetic VPN DNS addresses

The host resolved `notebooklm-pa.googleapis.com` to `198.20.0.132` through a `utun` VPN interface.
That synthetic route produced malformed upstream HTTP/2 handshakes in the standalone recorder.
Pinning a public IP obtained through DNS-over-HTTPS fixed upstream TLS and HTTP/2.

### HTTP Toolkit raw export

HTTP Toolkit's standard UI successfully displays the decoded HTTP/2 gRPC calls. Raw request export
is marked as a Pro feature. The working recorder uses Mockttp directly instead of bypassing that
product gate.

## Troubleshooting checklist

### App asks for sign-in repeatedly

1. Disconnect interception.
2. Remove global proxy settings from step 3.
3. Force-stop and relaunch NotebookLM.
4. Complete sign-in with a direct connection.
5. Re-enable interception only after the notebook list appears.

Do not clear app data unless losing the current login is acceptable.

### Companion says connected, but no NotebookLM calls appear

Check all four layers:

```bash
adb shell settings get global http_proxy
adb shell pm list packages -U tech.httptoolkit.android.v1
adb shell iptables -t nat -S OUTPUT | rg '8000|8081'
lsof -nP -iTCP:8081 -sTCP:LISTEN
```

The expected state is:

- global proxy is `null`;
- the HTTP Toolkit companion is installed;
- one companion-UID DNAT rule points 8000 to 8081; and
- Node is listening on host port 8081.

Force-stop and relaunch NotebookLM after correcting any layer.

### TLS errors or blank app screen

1. Confirm both trust indicators are enabled in the companion.
2. Verify the HTTP Toolkit CA in the NotebookLM mount namespace using step 4.
3. Re-resolve the public upstream IP.
4. Re-run the `curl --resolve` HTTP/2 check.
5. Restart the recorder, then restart NotebookLM.

### Recorder starts but writes framing errors

The recorder preserves the body as an owner-readable `.raw` file. Check `content-type`, compression,
and whether the endpoint uses streaming or a gRPC variant before changing the parser. Do not print
the raw body into issue logs.

## References

- [Android Conscrypt APEX module](https://source.android.com/docs/core/ota/modular-system/conscrypt)
- [HTTP Toolkit: Android interception](https://httptoolkit.com/docs/guides/android/)
- [HTTP Toolkit: Android 14 system CA injection](https://httptoolkit.com/blog/android-14-install-system-ca-certificate/)
- [Mockttp API](https://httptoolkit.github.io/mockttp/)
