"""Private chat-feature package (facade + helpers unified).

Cohesive cluster promoted from the former flat ``_chat*.py`` modules (issue #1328).
Unlike the other promoted clusters, the ``ChatAPI`` facade (formerly ``_chat.py``) is
moved *into* this package as :mod:`._chat.api` to resolve the package/module name
collision; it is re-exported here so existing references such as
``from notebooklm._chat import ChatAPI`` keep resolving unchanged.

No package-init import cycle exists: the dependency direction is strictly one-way
(``api`` imports the sibling helpers; none of them imports ``api`` or this package
``__init__``).
"""

from .api import ChatAPI

__all__ = ["ChatAPI"]
