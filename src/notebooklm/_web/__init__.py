"""Private implementation package for the batchexecute web backend.

Phase A moves web-wire decoding, request construction, and concrete namespace
implementations into this package. Backend-neutral public types and namespace
bases remain outside it. Imports across this boundary are constrained by
``tests/_guardrails/test_backend_boundaries.py``.
"""
