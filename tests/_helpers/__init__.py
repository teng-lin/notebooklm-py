"""Shared test helpers.

Lives alongside ``tests/_fixtures/`` — both directories are underscore-prefixed
helper packages under the regular ``tests`` package and should be imported via
fully-qualified ``tests._helpers`` / ``tests._fixtures`` paths. Modules here are
helpers that are NOT pytest fixtures (no ``@pytest.fixture`` decorators);
fixtures continue to live in ``tests/_fixtures/``.
"""
