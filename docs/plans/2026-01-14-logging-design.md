# Comprehensive Logging Design

## Overview

Add DEBUG-level logging across RPC layer, client APIs, and CLI to enable debugging of the undocumented NotebookLM API.

## Logger Hierarchy

```
notebooklm (root)
├── notebooklm._core      # RPC execution, HTTP requests
├── notebooklm._sources   # Source operations
├── notebooklm._notebooks # Notebook operations
├── notebooklm._artifacts # Artifact operations
├── notebooklm._chat      # Chat operations
├── notebooklm.rpc.encoder # Request encoding
├── notebooklm.rpc.decoder # Response parsing
└── notebooklm.cli.*      # CLI commands
```

## Level Strategy

- `DEBUG` - All verbose details: RPC payloads, method parameters, raw responses
- `INFO` - Major operations (create, delete) - existing usage preserved
- `WARNING` - Recoverable issues - existing usage preserved
- `ERROR` - Exceptions and failures

## Implementation Priority

1. **RPC layer** (highest value for API debugging)
   - `rpc/encoder.py`: Method ID, encoded payload
   - `rpc/decoder.py`: Raw response, decoded structure

2. **Core layer**
   - `_core.py`: HTTP timing, status codes, request counter

3. **Client APIs**
   - Entry/exit logging for public methods

4. **CLI** (lowest priority)
   - Command invocation logging

## Safety Considerations

- Never log auth tokens, cookies, or credentials
- Large payloads (audio/video) log metadata only
- Sensitive fields should be redacted

## Usage

```python
import logging
logging.getLogger("notebooklm").setLevel(logging.DEBUG)
```

## Optional Enhancement

Add `include_rpc` flag to `configure_logging()` for separate RPC verbosity control.
