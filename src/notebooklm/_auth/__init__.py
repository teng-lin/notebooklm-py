"""Private authentication implementation package.

Deliberately empty. The package used to eagerly re-export six submodules
(``extraction``, ``headers``, ``keepalive``, ``paths``, ``refresh``,
``tokens``) on the stated rationale that this is what makes
``from notebooklm._auth import refresh`` work. That rationale was wrong: the
import system resolves ``from <package> import <submodule>`` by importing the
submodule itself, so the re-export was never load-bearing for any in-tree
consumer (all of them use either that form or ``import notebooklm._auth.X``).
What the re-export *did* buy was bare attribute access
(``import notebooklm._auth`` then ``notebooklm._auth.refresh``) — a form no
consumer in ``src/``, ``tests/`` or ``scripts/`` uses.

Deleting it changes no import-time footprint: ``import notebooklm`` already
loads every ``_auth`` submodule through the facade, identically before and
after (verified by comparing ``sys.modules`` on both revisions). The reason to
delete it is that it was a false rationale a future reader would have believed,
not a cost it was imposing.
"""
