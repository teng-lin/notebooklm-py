"""MCP integration tests using VCR cassettes.

These tests drive the FastMCP tool surface end-to-end through a REAL
:class:`~notebooklm.client.NotebookLMClient` (not the unit-test mock), with VCR
intercepting HTTP, proving the full ``MCP tool -> _app/client -> recorded RPC``
path against EXISTING cassettes.

This is the MCP counterpart of ``tests/integration/cli_vcr/``: it reuses the
SAME cassettes the CLI VCR tests replay (NEVER re-recorded —
``NOTEBOOKLM_VCR_RECORD`` stays unset). The ``notebooklm_vcr`` matcher keys on
``rpcids`` + decoded-body *shape* (see ``tests/vcr_config.py``), so a tool whose
code path issues the same batchexecute RPC set as the sibling CLI command
matches the recording with no re-record. Every tool is invoked with a FULL
canonical UUID so the name->id resolver takes its full-UUID fast path and never
adds an extra ``LIST_*`` RPC the cassette does not have.

What this catches that the unit suite does not:

- The real-decode -> MCP-wire-shape serialization (``to_jsonable`` over a typed
  result decoded from a recorded RPC payload, not a hand-built mock).
- MCP-vs-CLI RPC-path parity: the tool's call set must match the recorded set,
  which the matcher enforces.
- The structured error projection from a REAL decoded RPC error (not a mock
  exception) onto the ``CODE: ... (retriable=...)`` MCP wire contract.
"""
