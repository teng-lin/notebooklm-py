"""Private Android transport implementation.

The package root deliberately imports no optional gRPC or protobuf runtime.
Concrete Android modules are loaded only by private assembly paths that need
them.
"""

from __future__ import annotations

__all__: list[str] = []
