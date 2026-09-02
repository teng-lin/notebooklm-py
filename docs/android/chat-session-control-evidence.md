# Android chat-session status and cancellation evidence

Issue [#2303](https://github.com/teng-lin/notebooklm-py/issues/2303) admits the
two chat-session control calls implemented by both public backends. The probes
used a disposable copied notebook and removed it after the status/cancel cycle.

## GetChatSessionStatus

The Web registry maps `oXwmh` to `GetChatSessionStatus`. Android gRPC accepts a
request with chat-session ID at tag 2 and returns generation token at tag 1 plus
status at tag 2. Idle is `{2: 1}`; a running Web-originated stream becomes
`{1: <opaque token>, 2: 2}` and returns to idle before the stream closes. The
same status flip was observed cross-transport while Web owned the stream.

The APK does not publish this method binding, so the conventional request and
response type names are classified as a Web-derived signature inference. The
live tag shapes, status values, and cross-transport behavior are the admission
basis.

## CancelGeneration

The 1.55.10 APK exposes the exact
`CancelGenerationRequest { RequestContext requestContext = 1; string
chatSessionId = 2; string agencySessionId = 3; }` and empty named response.
Android gRPC accepted `{1: context, 2: chat_session_id}`. An unowned session
returned gRPC `PERMISSION_DENIED` (7), which the Android transport preserves as
its typed authorization error.

Cancellation stops server emission and persists the active turn as cancelled,
but the Web HTTP response does not close. More importantly, Google only cancels
streams whose originating `GenerateFreeFormStreamedRequest.requestContext`
carries `clientType=WEB` (2); `ANDROID_APP` (3) streams continue. The Android
adapter therefore retains Android metadata/provenance but deliberately sends
the Web client type for chat generation and cancellation. This is isolated to
the chat control path and makes the public `chat.cancel()` contract effective
on both backends.
