# Authentication and cookie lifecycle

**Status:** Active
**Last Updated:** 2026-09-02

<a id="tldr"></a>

NotebookLM does not offer this project a public OAuth or service-account API.
The Web backend therefore uses Google browser-session cookies. Treat a
`storage_state.json` export as a full-account credential: do not commit it,
send it to third parties, or expose it in logs. Keep profile directories and
credential files readable only by the account that runs the client.

## Choose an authentication method

| Use case | Recommended method |
|---|---|
| Interactive desktop use | `notebooklm login` |
| Reuse an already signed-in local browser | `notebooklm login --browser-cookies <browser>` (requires the `cookies` extra) |
| Short-lived CI job | provide a private `NOTEBOOKLM_AUTH_JSON` secret |
| Long-lived Web worker, server, or unattended job | bootstrap a master-token profile, then use its file-backed storage |
| Android backend | install the `android` extra and use a profile containing `master_token.json` |

`NOTEBOOKLM_AUTH_JSON` is deliberately ephemeral: it has no writable backing
file, so it cannot persist refreshed cookies or use file-backed recovery.

The Android backend does not authenticate with the Web cookie jar. At client
open it reads the profile's durable master token and mints short-lived Android
bearer credentials. Bootstrap the profile with
`notebooklm login --master-token --account you@example.com`; install
`notebooklm-py[android]` where the Android client runs. The bootstrap flow also
needs `gpsoauth` (provided by `headless` and `android` extras) and, unless an
`--oauth-token` is supplied, browser support to capture the one-time token.

> **Security warning:** a master token is a durable, full-account credential.
> Use a dedicated account where possible, restrict the file to its owner, and
> never put it in an image, repository, or command line.

## Cookie requirements for the Web backend

Use a complete browser export. The loader requires `SID` and
`__Secure-1PSIDTS`; incomplete exports are a common cause of authentication
failures. It also warns when the secondary binding is absent. The warning is
kept non-fatal for compatibility with unverified SSO and Workspace flows, but
re-authenticating with a complete export is the preferred remedy.

The executable policy below is intentionally kept in sync with the runtime.
It distinguishes the strict validation rule from the weaker precondition used
only to decide whether a rotation attempt is worthwhile.

```python
MINIMUM_REQUIRED_COOKIES = {"SID", "__Secure-1PSIDTS"}


def _has_valid_secondary_binding(cookie_names: set[str]) -> bool:
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID", "LSID"} <= cookie_names


def _has_rotatable_secondary_binding(cookie_names: set[str]) -> bool:
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID"} <= cookie_names
```

<a id="25-four-timers-people-confuse"></a>

Cookie names alone are not a guarantee that a session will work. Google can
rotate or revoke a session based on account policy and risk signals. Do not
rely on cookie expiry timestamps as a health check; run `notebooklm auth check
--test` when an application needs a live Web-auth probe.

<a id="33-empirical-cookie-requirements"></a>

## Keeping a file-backed Web session fresh

<a id="4--the-recovery-ladder"></a>

The client makes a best-effort cookie-rotation request while it is open.
Long-running SDK clients can add a periodic keepalive:

```python
from notebooklm import NotebookLMClient

async with NotebookLMClient.from_storage(keepalive=600) as client:
    ...
```

MCP and REST servers use this 600-second keepalive by default. For an idle
profile, run `notebooklm auth refresh` from an OS scheduler. It refreshes a
file-backed Web session and writes any updated cookies; use `--verify` when a
successful live check is required. A zero exit means the refresh path completed
without an error, not that a cookie necessarily changed.

`auth refresh` cannot update `NOTEBOOKLM_AUTH_JSON`. If a Web session is no
longer usable, re-run `notebooklm login`, re-extract browser cookies, or use a
master-token profile. `NOTEBOOKLM_REFRESH_CMD` is an optional operator-supplied
recovery hook; its command must safely rewrite the specified storage file and
must not print secrets.

When several processes share one file-backed profile, coordinate operationally
where possible. The library protects writes, but a process can still hold an
older in-memory session until its next reload or refresh.

## Browser-cookie imports

`notebooklm login --browser-cookies <browser>` requires
`pip install "notebooklm-py[cookies]"`. Browser support and access vary by
platform and browser encryption settings; if extraction is incomplete, use
interactive login or another supported browser instead of hand-editing a small
cookie subset. For multi-account browsers, inspect accounts first and select
the intended account explicitly.

`notebooklm auth import-cookies` accepts a Playwright storage-state object or a
cookie list. Import only a file you created yourself, delete temporary exports
afterward, and unset `NOTEBOOKLM_AUTH_JSON` first because inline auth takes
precedence.

## Compatibility note for Python callers

`CookieJar` is an immutable, ordered sequence of cookie rows, never a Mapping
and never the managed client's live jar. Use `AuthTokens.jar` only for
bootstrap-cookie questions; use managed `NotebookLMClient` request APIs for
requests. The v0.x `AuthTokens` cookie projections remain compatibility
surfaces while callers migrate to the v1 `initial_cookies` field.

## Troubleshooting

- Use `notebooklm auth check` for local file/cookie validation, and add
  `--test --passive` for a read-only live probe.
- For Web authentication failures, re-run `notebooklm login` or import a fresh,
  complete browser export.
- For Android failures, confirm both `notebooklm-py[android]` and a valid
  profile `master_token.json`; cookie-only and inline-cookie auth are not
  Android credentials.
- See [troubleshooting.md](troubleshooting.md#authentication-errors) for
  command-level remediation.
