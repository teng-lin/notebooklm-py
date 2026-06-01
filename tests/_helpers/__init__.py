"""Shared test helpers.

Lives alongside ``tests/_fixtures/`` — both directories are
underscore-prefixed so pytest auto-discovers them on ``sys.path`` via the
rootdir conftest mechanism. Modules here are helpers that are NOT pytest
fixtures (no ``@pytest.fixture`` decorators); fixtures continue to live in
``tests/_fixtures/``.
"""
