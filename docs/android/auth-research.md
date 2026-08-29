# Google Play services authentication for NotebookLM Android

**Status:** Live-validated research

**Last verified:** 2026-08-05

**Scope:** How the official NotebookLM Android app obtains OAuth access tokens, what
microG's GmsCore reveals about the protocol, and whether `gpsoauth` can mint an equivalent
token for `notebooklm-py` without patching the APK or installing microG.

## Conclusion

Yes: the existing `gpsoauth` dependency can mint a bearer accepted by NotebookLM's Android
gRPC backend. We do **not** need to copy a token generator out of microG or replace Google Play
services for the Python client.

microG is nevertheless the best readable reference implementation of the classic Android
Google-auth broker. It confirms that the broker:

1. identifies the calling app by package name and signing-certificate digest;
2. combines that identity with an `oauth2:` scope string to form the token cache key;
3. uses a durable account credential (the master token, called the LST in current GMS) to ask
   Google's Android auth endpoint for a short-lived token; and
4. returns and caches the access token, not the durable credential, for the app.

The live official implementation has evolved beyond microG. Google Play services 25.34.34 can
fetch an intermediate token and locally attenuate it into a `ya29.m.` macaroon. `gpsoauth`
instead obtains a conventional server-issued `ya29` token. Both were accepted by the NotebookLM
backend in this test.

Use these identities for their distinct jobs:

| Goal | `app` / certificate | `service` |
|---|---|---|
| Call NotebookLM Android gRPC | NotebookLM Android identity | the full Android scope bundle below |
| Mint browser cookies through `OAuthLogin` | Chromecast identity already in `_auth/master_token.py` | `oauth2:https://www.google.com/accounts/OAuthLogin` |

The Chromecast identity is a web-cookie bridge. It is not required for direct calls to the
Android gRPC API.

## Evidence and versions

This report distinguishes three evidence levels:

- **Public platform behavior:** Android, Google, microG, and `gpsoauth` sources linked below.
- **Local reverse engineering:** decompilation of the installed proprietary GMS APK. Class names
  are obfuscated and specific to the tested build.
- **Live validation:** token introspection and one read-only gRPC request. No token, account name,
  or response payload was printed or persisted.

| Component | Tested version |
|---|---|
| Android virtual device | Android 16 / API 36.1, `google_apis`, ARM64 |
| Google Play services | `25.34.34 (260400-800653487)`, version code `253434035` |
| NotebookLM Android | package `com.google.android.apps.labs.language.tailwind` |
| NotebookLM signing SHA-1 | `a3382adf91991e6ef1e7e7de309c1febfedf3283` |
| microG GmsCore | commit [`9a206ae`](https://github.com/microg/GmsCore/tree/9a206ae115d6f4d99300def2aea447332ac84260) |
| `gpsoauth` | `2.0.0`, commit [`9cd120d`](https://github.com/simon-weber/gpsoauth/tree/9cd120d127ba728e273dd7a8aa7356431fc60f1f) |

The Android framework documents the high-level contract: `AccountManager.getAuthToken()` returns
a cached token when possible, otherwise asks the authenticator to create one, and returns the
result in `KEY_AUTHTOKEN`. The token type is authenticator-defined. See the
[`AccountManager` API](https://developer.android.com/reference/android/accounts/AccountManager#getAuthToken(android.accounts.Account,%20java.lang.String,%20android.os.Bundle,%20boolean,%20android.accounts.AccountManagerCallback%3Candroid.os.Bundle%3E,%20android.os.Handler)).
Google's `GoogleAuthUtil.getToken()` exposes that behavior for Google accounts and documents an
`oauth2:` prefix with space-separated scopes. It is now deprecated for normal app development.
The NotebookLM APK ships both this API and a feature-gated migration to the newer AANG
`GoogleAuthClient`, so the exact entry point can vary with configuration. See
[`GoogleAuthUtil`](https://developers.google.com/android/reference/com/google/android/gms/auth/GoogleAuthUtil#getToken(android.content.Context,%20android.accounts.Account,%20java.lang.String,%20android.os.Bundle)).

## End-to-end token path

```text
NotebookLM app
  |  GoogleAuthUtil / GetToken Binder call
  |  account + "oauth2:<scopes>"
  v
Google Play services auth broker
  |  resolve Binder UID -> real package -> installed certificate
  |  cache key = package:certificate:service
  |
  +-- cache hit ------------------------------+
  |                                           |
  +-- current GMS: LST -> intermediate token  |
  |                  -> local attenuation     |
  |                  -> ya29.m. bearer        |
  |                                           |
  +-- classic/microG: LST -> Android auth ----+
                      endpoint -> ya29 bearer |
                                              v
NotebookLM app -> Authorization: Bearer <token> -> Android gRPC backend
```

Android itself checks that the caller-supplied package belongs to the Binder UID. For custom-token
authenticators it also includes a digest of the caller's installed certificate in the cache key.
The current AOSP implementation is visible in
[`AccountManagerService.getAuthToken()`](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/services/core/java/com/android/server/accounts/AccountManagerService.java#3095).
This is why an arbitrary Android app cannot simply claim to be NotebookLM when calling the normal
Binder API.

Google's public client-authentication documentation likewise identifies the Android client by its
package and signing-certificate SHA-1: [Client authentication](https://developers.google.com/android/guides/client-auth).

## What the live Google Play services build does

### Entry point and caller binding

The installed APK declares:

- `com.google.android.gms.auth.GetToken`, exported as the Google auth Binder service;
- `GoogleAccountAuthenticatorService`, the `com.google` account authenticator; and
- `android.accounts.AccountAuthenticator.customTokens=1`.

The legacy-compatible call path retained by build `253434035` is:

```text
ChimeraGetToken
  -> wrh (IAuthManagerService Binder implementation)
  -> adlw (request validation and TokenRequest construction)
  -> adlx/adlz (cache, intermediate-token, and attenuation path)
  -> adlt (Android auth HTTP request)
  -> adlu (response, cache, and LST rotation handling)
```

`wrh` gets the calling UID from Binder, resolves packages belonging to that UID, and obtains the
installed package signature. `adlw` then constructs a `TokenRequest` containing the real app
description. This matches AOSP's anti-masquerading behavior rather than trusting an arbitrary
package string from the app.

NotebookLM also contains `GoogleAuthClient`, `GetTokenRequest`, and
`IGoogleAuthAangService` classes plus the feature flag
`enableGoogleAuthClientForGetToken1p`. The GMS APK exports the matching AANG service, whose token
operation shares the intermediate-token and attenuation machinery described below. The live cache
proves the resulting token variant, but this study did not force the app's feature flag to prove
which client entry point was selected for each request.

### The exact NotebookLM cache key

The live account token cache contained this token type:

```text
com.google.android.apps.labs.language.tailwind:
a3382adf91991e6ef1e7e7de309c1febfedf3283:
oauth2:https://www.googleapis.com/auth/account_settings_mobile
 https://www.googleapis.com/auth/cclog
 https://www.googleapis.com/auth/drive
 https://www.googleapis.com/auth/experimentsandconfigs
 https://www.googleapis.com/auth/labs-tailwind
 https://www.googleapis.com/auth/notifications
 https://www.googleapis.com/auth/photos.image.readonly
 https://www.googleapis.com/auth/supportcontent
 https://www.googleapis.com/auth/userinfo.email
 https://www.googleapis.com/auth/userinfo.profile
```

The line breaks above are only for readability. The stored key is one string, with the scopes
separated by spaces.

The cached value had prefix `ya29.m.` and length 585. Only its prefix and length were queried. No
credential value was printed.

### Durable credential storage

The long-lived credential is **not** the `ya29` access token. Current GMS calls it the long-lived
token (LST); the credentials produced by the EmbeddedSetup flow commonly have the `aas_et/`
prefix and are called master tokens by `gpsoauth`.

In the tested image:

- the `password` column for the Google account in `/data/system_ce/0/accounts_ce.db` was null;
- `acst.i(account)` first read the LST from `GoogleAccountDataStore` and only fell back to
  `AccountManager.getPassword()`;
- `acst.r(account, lst)` wrote the LST to that datastore and then cleared the AccountManager
  password; and
- `acsh` identified the backing file as
  `/data/user/0/com.google.android.gms/files/authaccount/shared/GoogleAccountDataStore.pb`.

That file was mode `0600`, owned by the GMS app UID, and 258 bytes in this test. It was not dumped.
Root can bypass the filesystem boundary, so it is technically extractable from a disposable
emulator, but automating that is a poor refresh strategy: the format is private, the LST can
rotate, and compromise of it is equivalent to compromise of a durable full-account credential.

### Current AANG and local attenuation path

The APK contains the internal method path:

```text
google.internal.android.auth.aang.v1.AangService/FetchGetTokenTokens
```

The decompiled `adlx`/`adlz` path checks an intermediate-token store, obtains a valid intermediate
token when needed, restricts it to the requested scopes, calculates an expiry caveat, and emits a
locally attenuated token prefixed `ya29.m.`. The code labels this a cached "LDAT Macaroon Access
Token." This explains why the official app's cached token differs from the conventional `ya29`
token returned by `gpsoauth`.

The same build retains a classic network path. `adlt` reads the LST and posts fields including
`Email`, `Token`, `service`, `app`, `client_sig`, `system_partition`, `has_permission`, and
sometimes `droidguard_results` to `https://android.googleapis.com/auth`. `adlu` processes the
response and can replace the stored LST (logged by the code as `Switching LST`).

This makes the practical boundary clear: reproducing AANG is unnecessary as long as Google's
classic auth exchange continues to return a bearer accepted by the target service.

## What microG implements

microG is a complete replacement broker, not a credential source. Its readable implementation
closely matches the classic branch still visible in current GMS:

- [`AuthManagerServiceImpl`](https://github.com/microg/GmsCore/blob/9a206ae115d6f4d99300def2aea447332ac84260/play-services-core/src/main/java/org/microg/gms/auth/AuthManagerServiceImpl.java#L116-L166)
  verifies the calling package and returns `TokenData` through the Play services-compatible Binder
  API.
- [`AuthManager.buildTokenKey()`](https://github.com/microg/GmsCore/blob/9a206ae115d6f4d99300def2aea447332ac84260/play-services-core/src/main/java/org/microg/gms/auth/AuthManager.java#L97-L110)
  constructs `packageName:firstSignatureDigest:service`, exactly matching the key observed for
  NotebookLM.
- [`AuthManager.requestAuth()`](https://github.com/microg/GmsCore/blob/9a206ae115d6f4d99300def2aea447332ac84260/play-services-core/src/main/java/org/microg/gms/auth/AuthManager.java#L313-L363)
  returns an unexpired cached token or builds a network request using the account password as its
  durable `Token`.
- [`AuthRequest`](https://github.com/microg/GmsCore/blob/9a206ae115d6f4d99300def2aea447332ac84260/play-services-base/core/src/main/java/org/microg/gms/auth/AuthRequest.java#L24-L100)
  posts the package, certificate, account, service, device, GMS version, and durable `Token` fields
  to `https://android.googleapis.com/auth`.
- [`LoginActivity`](https://github.com/microg/GmsCore/blob/9a206ae115d6f4d99300def2aea447332ac84260/play-services-core/src/main/java/org/microg/gms/auth/login/LoginActivity.java#L333-L392)
  extracts the short-lived `oauth_token` cookie from Google's embedded login, exchanges it, and
  stores the response `Token` as the account password.

microG also enforces caller identity. Its normal authenticator resolves the actual requesting
package. Package/certificate override is a separate, user-approved path; see
[`AccountAuthenticator`](https://github.com/microg/GmsCore/blob/9a206ae115d6f4d99300def2aea447332ac84260/play-services-core/src/main/java/org/microg/gms/auth/loginservice/AccountAuthenticator.java#L93-L139).

Therefore:

- installing microG can provide its legacy-compatible token broker to a compatible Android app
  after an account is provisioned, but NotebookLM's feature-gated AANG client path would need
  separate compatibility testing;
- extracting its `AuthRequest` logic does not eliminate the need for a master token/LST; and
- for a Python client, using `gpsoauth` is the smaller equivalent of extracting and maintaining
  those Java classes.

microG does not currently reproduce the proprietary AANG intermediate-token/local-attenuation
path seen in this GMS build. The live result shows that NotebookLM does not require that token
variant.

## `gpsoauth`: exact NotebookLM parameters

`gpsoauth.perform_oauth()` implements the same essential form exchange, using the legacy
`https://android.clients.google.com/auth` host and an `EncryptedPasswd` field. Its implementation
is in [`gpsoauth/__init__.py`](https://github.com/simon-weber/gpsoauth/blob/9cd120d127ba728e273dd7a8aa7356431fc60f1f/gpsoauth/__init__.py#L214-L263).

Use the NotebookLM identity for an Android bearer:

```python
import gpsoauth

scopes = [
    "https://www.googleapis.com/auth/account_settings_mobile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "https://www.googleapis.com/auth/labs-tailwind",
    "https://www.googleapis.com/auth/notifications",
    "https://www.googleapis.com/auth/photos.image.readonly",
    "https://www.googleapis.com/auth/supportcontent",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

result = gpsoauth.perform_oauth(
    email,
    master_token,
    android_id,  # keep this stable across refreshes
    service="oauth2:" + " ".join(scopes),
    app="com.google.android.apps.labs.language.tailwind",
    client_sig="a3382adf91991e6ef1e7e7de309c1febfedf3283",
)
bearer = result["Auth"]  # never print or persist this unnecessarily
```

For Android gRPC, send it as:

```http
Authorization: Bearer <Auth>
```

Do not follow `gpsoauth`'s generic docstring suggestion to use `GoogleLogin auth=...` for this
OAuth2/gRPC case.

The `email`, `master_token`, and stable `android_id` already stored in this repository's
`master_token.json` profile are sufficient. See
[`_auth/master_token.py`](../../src/notebooklm/_auth/master_token.py) and
[`ADR 0023`](../adr/0023-master-token-headless-auth.md) for the existing bootstrap and security
model. Do not reuse the Chromecast `app` and certificate for direct Android API access.

## Live validation results

All validation used a locally stored master token in memory and suppressed HTTP debug logging.
The scripts printed metadata only.

### Full Android scope bundle

| Check | Result |
|---|---|
| `gpsoauth.perform_oauth` | success |
| Token variant | conventional `ya29`, not `ya29.m.` |
| Token length | 432 |
| Google token-info response | HTTP 200 |
| Requested scopes missing | none |
| Additional normalized scopes | `email`, `openid`, `profile` |
| Remaining lifetime at inspection | 4,859 seconds |

The minted bearer was then used for a read-only Android RPC:

```text
POST https://notebooklm-pa.googleapis.com/
  google.internal.labs.tailwind.orchestration.v1.
  LabsTailwindOrchestrationService/GetOrCreateAccount
```

An empty protobuf request is valid for this method because all request fields are optional. With
only the bearer and standard gRPC headers, the server returned:

```text
HTTP/2 200
grpc-status: 0
response frame: 59 bytes
```

This proves the token was accepted by the Android backend; token introspection alone would not.

### NotebookLM-only scope

The least-privilege experiment used only:

```text
oauth2:https://www.googleapis.com/auth/labs-tailwind
```

Google minted a token containing exactly that scope, but the same read-only RPC returned
`grpc-status: 7` (`PERMISSION_DENIED`). Consequently, the `labs-tailwind` scope by itself is not a
working substitute for the Android app's real scope bundle. Use the observed full bundle unless a
separate, controlled minimization study proves a smaller combination across the required RPCs.

### Official GMS token versus `gpsoauth` token

| Property | Official app via GMS 25.34 | Direct `gpsoauth` |
|---|---|---|
| Durable input | private LST/master token | stored `aas_et/` master token |
| Issuance path | AANG intermediate token + local attenuation, with classic fallback | classic Android auth form endpoint |
| Bearer variant observed | `ya29.m.` | non-`ya29.m.` `ya29` |
| Package/cert/service identity | NotebookLM / NotebookLM SHA-1 / full scopes | same |
| Accepted by NotebookLM gRPC | yes, observed through app capture | yes, directly validated |

## What can and cannot be extracted

### Short-lived app bearer

On a rooted, unlocked emulator the cached `ya29.m.` token is present in Android account storage.
It can be read by root, but this is useful only for a short experiment: it expires, can be cleared,
and is not a renewable credential. Capturing it in logs or shell history creates avoidable risk.

### Master token / LST

The durable credential is in GMS-private `GoogleAccountDataStore.pb` on this build, not in the
AccountManager password column. Root makes extraction technically possible, but the preferred
path is the existing explicit EmbeddedSetup bootstrap that writes `master_token.json` with mode
`0600`. That path is understood, testable, and independent of a proprietary protobuf schema.

A short-lived bearer cannot be converted back into the master token. The direction is one-way:

```text
oauth_token from interactive login -> master token/LST -> renewable access tokens
```

### Code from microG

The useful part to "extract" is the protocol model, not a binary component:

- endpoint and form fields from `AuthRequest`;
- caller/package/certificate binding from `AuthManager`;
- cache and expiry behavior; and
- login-to-master-token exchange from `LoginActivity`.

`gpsoauth` already supplies the minimum Python implementation. Copying microG code would add an
HTTP client, Android context/device collectors, AccountManager storage, Binder services, consent
UI, and compatibility layers that `notebooklm-py` does not need.

## Safe reproduction checks

The following commands inspect metadata without exposing credentials.

```bash
# Installed versions and auth components
adb shell dumpsys package com.google.android.gms \
  | rg 'versionName=|versionCode=|GoogleAccountAuthenticatorService|auth.GetToken'

# Token type, safe prefix, and length only; never select the full authtoken column
adb shell 'su 0 sqlite3 /data/system_ce/0/accounts_ce.db \
  "SELECT type, substr(authtoken,1,7), length(authtoken) \
   FROM authtokens \
   WHERE type LIKE '\''com.google.android.apps.labs.language.tailwind:%'\'';"'

# Confirm that the legacy AccountManager password was migrated away
adb shell 'su 0 sqlite3 /data/system_ce/0/accounts_ce.db \
  "SELECT password IS NULL FROM accounts WHERE type='\''com.google'\'';"'

# Locate the private store, but do not print it
adb shell 'su 0 stat \
  /data/user/0/com.google.android.gms/files/authaccount/shared/GoogleAccountDataStore.pb'
```

For the proprietary implementation, pull and decompile only the relevant installed APK in a
temporary directory:

```bash
adb pull /product/priv-app/PrebuiltGmsCore/PrebuiltGmsCore.apk /tmp/PrebuiltGmsCore.apk

jadx --single-class 'com.google.android.gms.auth.account.authenticator.GoogleAccountAuthenticatorService' \
  --single-class-output /tmp/gms-auth.java \
  /tmp/PrebuiltGmsCore.apk

strings -a /tmp/PrebuiltGmsCore.apk \
  | rg 'android.googleapis.com/auth|FetchGetTokenTokens|GoogleAccountDataStore'

# Confirm that NotebookLM ships both the legacy and AANG client paths
strings -a notebooklm.apk/base.apk \
  | rg 'GoogleAuthUtil|GoogleAuthClient|enableGoogleAuthClientForGetToken1p'
```

Obfuscated class names change between releases. Search by stable service names, URLs, request-field
strings, and log messages rather than assuming `adlt`, `adlu`, or `acst` will remain stable.

## Implemented `notebooklm-py` transport

The client does not install microG or parse GMS private storage. The Android transport now:

1. mints the mobile bearer internally beside the existing master-token code;
2. reuses `_require_gpsoauth()`, `_quiet_gpsoauth_logging()`, and the stored stable Android ID;
3. uses the NotebookLM package, certificate, and full observed scope bundle exactly as above;
4. keeps the bearer in memory with its expiry and never emits it in normal logs;
5. invalidates and remints once after gRPC `UNAUTHENTICATED` (`16`);
6. treats `PERMISSION_DENIED` (`7`) as a scope/account/feature problem rather than an expiry retry;
   and
7. validates through the lifecycle-bound Android session before typed namespace calls use the
   reduced generated schema under `src/notebooklm/_android/proto/`.

## Limits and risks

- The Android auth endpoint and NotebookLM Android gRPC API are undocumented internal interfaces.
  They can change without notice and may be subject to Google account or product terms.
- One account and one GMS/app build were validated. Enterprise, supervised, Advanced Protection,
  or region-restricted accounts may behave differently.
- The original auth study used only read-only `GetOrCreateAccount`. Later qualified implementation
  probes exercised writes only on disposable copies or reversible account state with `finally`
  restoration; those results are recorded in the focused Android evidence reports.
- The full scope bundle grants broader Google access than the product-specific scope alone. Use a
  dedicated test account and protect the master token as a full-account credential.
- A rooted emulator defeats Android's normal credential isolation. Never use this workflow on a
  personal phone or primary account.

For the transport and protobuf work that follows authentication, see
[Capturing the NotebookLM Android gRPC API](capture.md) and
[NotebookLM Android API endpoints and message shapes](endpoints.md).
