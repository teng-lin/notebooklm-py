# Capturing NotebookLM Android traffic

How to intercept the NotebookLM Android app's HTTPS traffic on an Android
emulator, so you can re-discover the obfuscated `batchexecute` RPC method IDs
when Google changes them (the #1 breakage class — see `rpc/types.py`).

## Why this is harder than a normal app

The app networks over **Cronet** (Chromium's stack, shipped via Google Play
Services) plus gRPC/OkHttp. Two consequences:

1. **Cronet ignores the Android system proxy.** Setting a Wi-Fi proxy does not
   route Cronet traffic to mitmproxy.
2. **Cronet does its own certificate verification** (BoringSSL), so trusting a
   CA is not enough on its own.

So the working recipe is: rooted emulator + CA in the **system** trust store +
a **Frida** hook that (a) redirects native sockets to the proxy and (b)
defeats BoringSSL cert verification. We use HTTP Toolkit's open-source
`frida-interception-and-unpinning` scripts for (a)/(b).

## Prerequisites (Homebrew)

```bash
brew install --cask android-commandlinetools   # sdkmanager, avdmanager, adb, emulator
brew install mitmproxy
uv tool install frida-tools                     # host-side `frida` CLI (or: pipx install frida-tools)

export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
export ANDROID_SDK_ROOT=$ANDROID_HOME
export PATH=$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH
```

You need a **rootable** system image — `google_apis` (NOT `google_apis_playstore`,
which is production-locked and cannot `adb root`). Android 11 (`android-30`)
matches the app's `minSdk 29`:

```bash
yes | sdkmanager --licenses
sdkmanager "system-images;android-30;google_apis;arm64-v8a" "emulator" "platform-tools"
```

## The APKs

The app ships as split APKs (base + arch + density). Values from the build
documented here:

| field | value |
|-------|-------|
| package | `com.google.android.apps.labs.language.tailwind` |
| version | `1.46.7.940945420` (versionCode 138238) |
| minSdk / abi | 29 / arm64-v8a |

Install **all** splits together with `install-multiple`; installing `base.apk`
alone fails at runtime (missing arch/density resources).

## One-time setup

### 1. Create the AVD

```bash
echo "no" | avdmanager create avd -n nblm \
  -k 'system-images;android-30;google_apis;arm64-v8a' -d pixel_5
```

### 2. Boot it writable-system

On Apple Silicon + recent macOS, use **`-gpu host`** (Metal). `swiftshader_indirect`
(software) crashes with `Failed to create window surface … EGL_BAD_SURFACE (12299)`.

```bash
emulator @nblm -writable-system -no-snapshot -no-boot-anim -gpu host &
adb wait-for-device
# wait for full boot:
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done
```

### 3. Generate the mitmproxy CA and install it into the *system* store

mitmproxy creates its CA on first run at `~/.mitmproxy/`. Android's system store
keys certs by the old-OpenSSL subject hash, filename `<hash>.0`:

```bash
mitmdump & sleep 4; kill %1                        # generate ~/.mitmproxy/ if absent
HASH=$(openssl x509 -inform PEM -subject_hash_old \
        -in ~/.mitmproxy/mitmproxy-ca-cert.pem -noout)   # e.g. c8750f0d
cp ~/.mitmproxy/mitmproxy-ca-cert.pem /tmp/$HASH.0

adb root
adb disable-verity && adb reboot                   # verity must be off to write /system
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done
adb root && adb remount
adb push /tmp/$HASH.0 /system/etc/security/cacerts/$HASH.0
adb shell chmod 644 /system/etc/security/cacerts/$HASH.0
```

> Emulator state is not persisted with `-no-snapshot`, so `/system` (the CA and
> verity-off state) is lost on every cold boot — you re-run the boot + cert
> steps each session (see [Capturing](#capturing-every-session) below). Drop
> `-no-snapshot` (and snapshot-save on exit) to make it stick instead.

### 4. Install the app

```bash
cd /path/to/notebooklm.apk
adb install-multiple -r base.apk \
  split_config.arm64_v8a.apk split_config.xxhdpi.apk split_config.xxxhdpi.apk
adb shell pm path com.google.android.apps.labs.language.tailwind   # verify
```

### 5. Push frida-server (version must match the host `frida` CLI)

```bash
FRIDA_VER=$(frida --version)                       # e.g. 17.15.4
curl -fsSL -o /tmp/fs.xz \
  https://github.com/frida/frida/releases/download/$FRIDA_VER/frida-server-$FRIDA_VER-android-arm64.xz
unxz -f /tmp/fs.xz
adb push /tmp/fs /data/local/tmp/frida-server
adb shell chmod 755 /data/local/tmp/frida-server
```

### 6. Get the interception scripts and point them at our CA + proxy

```bash
mkdir -p frida-scripts/android
BASE=https://raw.githubusercontent.com/httptoolkit/frida-interception-and-unpinning/main
for f in config.js native-connect-hook.js native-tls-hook.js; do
  curl -fsSL "$BASE/$f" -o "frida-scripts/$f"
done
for f in android-certificate-unpinning.js android-certificate-unpinning-fallback.js \
         android-proxy-override.js android-system-certificate-injection.js; do
  curl -fsSL "$BASE/android/$f" -o "frida-scripts/android/$f"
done
```

Edit `frida-scripts/config.js`:

- `CERT_PEM` — paste the contents of `~/.mitmproxy/mitmproxy-ca-cert.pem`.
  **This must be the same CA you pushed to the system store** (verify with
  `openssl x509 -noout -fingerprint -sha256`; all three — proxy, system store,
  config.js — must match).
- `PROXY_HOST = '10.0.2.2'` — the host loopback as seen from inside the emulator.
- `PROXY_PORT = 8080`.

## Capturing (every session)

With `-no-snapshot` the emulator cold-boots fresh, so `/system` (the CA + the
verity-off state) is gone every time — but the AVD, the installed app, and
frida-server live on `userdata` and survive. So each session is: **boot the
emulator → re-inject the CA → start the three capture moving parts** (the proxy
on the host, frida-server on the device, and the Frida client injecting the
scripts as the app launches).

```bash
# 0. host: launch the emulator writable-system and wait for full boot
#    (-gpu host on Apple Silicon; see One-time setup §2 for why)
emulator @nblm -writable-system -no-snapshot -no-boot-anim -gpu host &
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done

# 1. host: re-inject the mitmproxy CA into the system store (lost on cold boot).
#    verity must be off to write /system, which needs a reboot, so do it first.
adb root
adb disable-verity && adb reboot
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done
adb root && adb remount
HASH=$(openssl x509 -inform PEM -subject_hash_old \
        -in ~/.mitmproxy/mitmproxy-ca-cert.pem -noout)
adb push ~/.mitmproxy/mitmproxy-ca-cert.pem /system/etc/security/cacerts/$HASH.0
adb shell chmod 644 /system/etc/security/cacerts/$HASH.0

# 2. host: start the proxy (mitmweb gives a browsable UI at http://127.0.0.1:8081)
mitmweb --listen-port 8080 &

# 3. device: start frida-server as root
adb shell "su 0 /data/local/tmp/frida-server &"      # or: adb root; adb shell /data/local/tmp/frida-server &

# 4. host: spawn the app under Frida with all hooks (order matters)
cd frida-scripts
frida -U \
  -l config.js \
  -l native-connect-hook.js \
  -l native-tls-hook.js \
  -l android/android-proxy-override.js \
  -l android/android-certificate-unpinning.js \
  -l android/android-certificate-unpinning-fallback.js \
  -f com.google.android.apps.labs.language.tailwind
```

> If you dropped `-no-snapshot` and snapshot-saved a booted+cert-injected state,
> skip steps 0–1 on subsequent sessions — `emulator @nblm` reloads the snapshot
> with the CA already in place.

`-f` spawns fresh so the hooks are in place before Cronet initializes. At the
`[Local::…]>` prompt type `%resume` if it doesn't auto-continue.

Now drive the app (sign in, open a notebook, ask a question, generate a
podcast). Watch requests land in the mitmweb UI.

## Reading the RPC IDs

`batchexecute` requests look like:

```
POST https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?rpcids=XXXXX&...
```

- The `rpcids` query param is the obfuscated method ID (what lives in
  `src/notebooklm/rpc/types.py`).
- The `f.req` form field holds the request envelope; the response is
  `)]}'`-prefixed chunked JSON.

Match the action you performed to its `rpcids`, then update `rpc/types.py`. In
mitmweb: filter the flow list with `~u batchexecute`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `EGL_BAD_SURFACE (12299)`, emulator crashes | Software GPU on Apple Silicon — use `-gpu host`. |
| `adb root` → "cannot run as root in production builds" | Wrong image — use `google_apis`, not `google_apis_playstore`. |
| `remount failed` | Run `adb disable-verity` then reboot before `adb remount`. |
| No traffic in mitmproxy at all | Cronet ignoring proxy — confirm `native-connect-hook.js` + `android-proxy-override.js` loaded; must spawn with `-f`, not attach. |
| TLS errors / app can't connect | CA mismatch — the fingerprints of proxy CA, `/system/etc/security/cacerts/<hash>.0`, and `config.js` `CERT_PEM` must all be identical. |
| `frida` fails to attach | Host `frida --version` must equal the pushed `frida-server` version. |
| App installs but crashes on open | Missing splits — use `install-multiple` with all APKs. |

## This setup's captured values

For reference, the working instance documented here used:

- AVD: `nblm` (`system-images;android-30;google_apis;arm64-v8a`, pixel_5)
- CA subject hash: `c8750f0d` → `/system/etc/security/cacerts/c8750f0d.0`
- CA SHA-256 fingerprint: `AE:F9:A4:D4:DC:E1:5C:2A:35:37:F9:7D:E7:FF:E1:4C:C5:07:76:9B:78:E9:B3:D7:36:20:70:24:8B:B0:98:B2`
- frida: host + `frida-server` `17.15.4`
- proxy: `10.0.2.2:8080` (mitmweb UI on `127.0.0.1:8081`)
