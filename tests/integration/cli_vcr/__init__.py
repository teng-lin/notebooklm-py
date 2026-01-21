"""CLI integration tests using VCR cassettes.

These tests exercise the full CLI → Client → RPC path using recorded HTTP responses.
Unlike unit tests (which mock the client), these use real NotebookLMClient instances
with VCR cassettes to replay recorded API responses.

This catches:
- Typos in client method calls from CLI commands
- Argument transformation bugs (e.g., --source-type youtube → SourceType.YOUTUBE)
- Response field access errors in output formatting
- Full integration between CLI parsing, client calls, and output formatting

Without:
- Real API calls (uses VCR cassettes)
- Authentication complexity (mock auth works with cassettes)
- Rate limiting or network flakiness
"""
