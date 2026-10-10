"""Shared backend-test lifecycle cleanup."""

import asyncio

import pytest

import jobs


@pytest.fixture(autouse=True)
def close_default_job_manager():
    """Close the application singleton after tests that use it indirectly.

    Production shutdown closes the manager through the FastAPI lifespan. A
    number of synchronous tests exercise the same singleton through
    ``asyncio.run`` and otherwise leave its aiosqlite worker attached to a
    loop that pytest closes immediately afterward.
    """
    yield
    manager = jobs.manager
    if manager._db is not None:
        asyncio.run(manager.stop())
