"""Replay a gzipped cassette through ``stream_post_with_size_cap``.

#769 was a real production bug — every RPC failed with ``Error -3 while
decompressing data: incorrect header check`` — that slipped through the
existing VCR suite because every cassette dropped ``Content-Encoding``
on record (``decode_compressed_response=True`` in
:mod:`tests.vcr_config`). This module is the cassette half of the
follow-up regression coverage for issue #773: a derived cassette under
``tests/cassettes/gzip_coverage/`` carries a gzipped response body
plus ``Content-Encoding: gzip``, replayed through the production
streaming transport. Reverting the
``_STRIP_HEADERS_ON_REBUFFER`` filter in #771 makes this test fail with
``httpx.DecodingError`` on the rebuild step.

A scoped :class:`vcr.VCR` instance with
``decode_compressed_response=False`` is used because vcrpy applies the
decode filter at cassette-LOAD time (not just on record) — see
``vcr.cassette.Cassette._load`` calling ``append`` which runs the
``before_record_response`` chain. The project's ``notebooklm_vcr``
instance leaves that flag on so cassettes stay diff-readable, so this
file pins a no-decode instance scoped to the gzipped cassettes only.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import vcr

from notebooklm._authed_transport import stream_post_with_size_cap

# Required by the tier-enforcement rule in :mod:`tests.integration.conftest`
# — every integration test must carry ``@pytest.mark.vcr``, be decorated
# with ``@notebooklm_vcr.use_cassette``, or opt out via
# ``@pytest.mark.allow_no_vcr``. We use a scoped ``vcr.VCR`` instance below
# (not ``notebooklm_vcr``), so the marker is the only path to satisfy the
# rule without spawning a no-op cassette on the project's default config.
pytestmark = pytest.mark.vcr


REPO_ROOT = Path(__file__).resolve().parents[2]
GZIP_COVERAGE_DIR = REPO_ROOT / "tests" / "cassettes" / "gzip_coverage"

# Pin a no-decode VCR instance: vcrpy applies ``decode_response`` filter at
# load time when ``decode_compressed_response`` is True, which would gunzip
# the cassette body and strip ``Content-Encoding`` before the test sees it,
# defeating the regression. The match rules mirror ``notebooklm_vcr`` so
# request matching stays consistent with how the source cassette was
# recorded.
# ``match_on`` deliberately omits the query string. The cassette has a
# single batchexecute interaction so the default ``method/scheme/host/
# port/path`` tuple identifies it uniquely. If more interactions are
# ever added under ``gzip_coverage/``, switch to the ``rpcids`` matcher
# (see ``tests.vcr_config._rpcids_matcher``) so the wrong recording can
# never be served on replay.
_gzip_replay_vcr = vcr.VCR(
    cassette_library_dir=str(GZIP_COVERAGE_DIR),
    record_mode="none",
    match_on=["method", "scheme", "host", "port", "path"],
    decode_compressed_response=False,
)


# Replayed POST coordinates pulled from the source recording
# ``tests/cassettes/artifacts_revise_slide.yaml``. We do not match on
# ``f.req`` or rpcids here because the cassette has a single batchexecute
# interaction — the default ``method/scheme/host/port/path`` matchers
# already pin it uniquely.
_REVISE_SLIDE_URL = (
    "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
    "?rpcids=KmcKPe"
    "&source-path=%2Fnotebook%2Fbb00c9e3-656c-4fd2-b890-2b71e1cf3814"
    "&f.sid=SCRUBBED&hl=en&rt=c"
)
_REVISE_SLIDE_BODY = (
    "f.req=%5B%5B%5B%22KmcKPe%22%2C%22%5B%5B2%5D%2C%5C%22e52e81c5-613b-43ec-"
    "8f45-66a120644a26%5C%22%2C%5B%5B%5B0%2C%5C%22Move%20the%20title%20up"
    "%5C%22%5D%5D%5D%5D%22%2Cnull%2C%22generic%22%5D%5D%5D&at=SCRUBBED_CSRF"
    "%3A1778848153629&"
)


@pytest.mark.asyncio
async def test_gzipped_cassette_replays_without_double_decode() -> None:
    """The streaming wrapper must rebuild the response without re-running
    the gzip decoder on already-decoded bytes.

    Without the ``_STRIP_HEADERS_ON_REBUFFER`` guard from #771, this test
    fails inside ``httpx.Response.__init__`` with
    ``DecodingError: Error -3 ... incorrect header check`` because the
    rebuilt response carries the upstream ``Content-Encoding: gzip`` over
    the decoded payload.
    """
    with _gzip_replay_vcr.use_cassette("artifacts_revise_slide_gzipped.yaml"):
        async with httpx.AsyncClient() as client:
            response = await stream_post_with_size_cap(
                client,
                _REVISE_SLIDE_URL,
                body=_REVISE_SLIDE_BODY,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
            )

    assert response.status_code == 200
    # Body decodes once — the XSSI prefix and a recognizable RPC envelope
    # marker confirm the gzip was decoded exactly once, not zero times
    # (raw gzip bytes) and not twice (DecodingError).
    assert response.text.startswith(")]}'")
    assert '"wrb.fr","KmcKPe"' in response.text
    # The misleading upstream ``Content-Encoding`` must not survive onto the
    # rebuilt response — otherwise downstream consumers that re-read or
    # re-stream would hit the same double-decode trap.
    assert "content-encoding" not in response.headers
