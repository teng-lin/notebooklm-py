"""Private chat-feature package (facade + helpers unified).

Cohesive cluster promoted from the former flat ``_chat*.py`` modules (issue #1328).
Unlike the other promoted clusters, the ``ChatAPI`` facade (formerly ``_chat.py``) is
moved *into* this package as :mod:`._chat.api` to resolve the package/module name
collision; its public names are re-exported here so existing references such as
``from notebooklm._chat import ChatAPI`` keep resolving unchanged.

Import order note: the helper submodules (``notes``/``wire``/``transport``) are imported
before ``api`` to keep the dependency direction one-way (``api`` -> helpers) and avoid any
package-init import cycle.
"""

from . import api, notes, transport, wire
from .api import ChatAPI, _extract_next_turn_content

__all__ = [
    "api",
    "notes",
    "transport",
    "wire",
    "ChatAPI",
    "_extract_next_turn_content",
]
