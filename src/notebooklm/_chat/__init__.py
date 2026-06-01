"""Private chat-feature package (facade + helpers unified).

Cohesive cluster promoted from the former flat ``_chat*.py`` modules (issue #1328).
Unlike the other promoted clusters, the ``ChatAPI`` facade (formerly ``_chat.py``) is
moved *into* this package as :mod:`._chat.api` to resolve the package/module name
collision; its public names are re-exported here so existing references such as
``from notebooklm._chat import ChatAPI`` keep resolving unchanged.

No package-init import cycle exists: the dependency direction is strictly one-way
(``api`` imports ``notes``/``wire``/``transport``; none of the helpers import ``api`` or
this package ``__init__``). Importing ``api`` therefore pulls in the helpers it needs
regardless of the order of the aggregating ``from . import ...`` line below, which ruff's
import sorter keeps alphabetised.
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
