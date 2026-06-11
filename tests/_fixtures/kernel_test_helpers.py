"""Test-only Kernel helpers for installing or replacing the live HTTP client.

The retired ``Kernel.http_client`` setter (``src/notebooklm/_kernel.py``; see
the constructor-DI notes in ``src/notebooklm/_runtime/init.py``) used to absorb
every post-construction ``core._kernel.http_client = ...`` swap. Production code
never used it; tests used it to substitute a ``MockTransport``-backed client
either before or after the client runtime opened.

The preferred replacement is constructor-time injection via the test client
shell's ``async_client_factory`` — that factory is forwarded into
``Kernel.__init__`` and applied by ``Kernel.open()`` so the live client carries
the test transport from birth. Most call sites can migrate to the factory
pattern.

A small minority of tests cannot use the factory cleanly:

- Tests that need to install a stand-in client BEFORE invoking the runtime open
  path (e.g. arbitration tests that stub out client opening entirely and only
  need a non-``None`` client for ``close()`` to operate on).
- Tests that need to swap the client AFTER ``open()`` returned its real
  transport but BEFORE driving the next operation.

Both classes use :func:`install_http_client_for_test` to perform the swap
explicitly. The helper writes to ``Kernel._http_client`` directly so the
test-only seam is concentrated in this module (instead of being scattered
across every call site as a property setter would invite). New uses
outside this module should prefer the constructor-injected factory path
unless they fall into one of the two cases above.
"""

from __future__ import annotations

import httpx

from notebooklm._kernel import Kernel


def install_http_client_for_test(
    kernel: Kernel,
    client: httpx.AsyncClient | None,
) -> None:
    """Install or clear the live HTTP client on a :class:`Kernel`.

    Mirrors the retired ``Kernel.http_client`` setter for test scenarios
    that cannot reach the live client through constructor-time
    ``async_client_factory`` injection. See this module's docstring for
    the migration guidance.

    Args:
        kernel: The :class:`Kernel` instance whose live HTTP client is
            being replaced. Typically reached via
            ``client._collaborators.kernel`` (or ``core._kernel`` in older
            test fixtures).
        client: The replacement ``httpx.AsyncClient`` (or ``None`` to
            clear the live client and force the next operation to fail
            with the pre-open ``RuntimeError`` from
            :meth:`Kernel.get_http_client`).
    """
    kernel._http_client = client
