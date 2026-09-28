"""Shared pytest config for all test modules."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Pin pytest-anyio to asyncio only.

    Without this, pytest-anyio parametrizes every @pytest.mark.anyio test
    against every importable backend (asyncio and trio). trio isn't a
    project dependency today, so this is a no-op in practice - but several
    tests use raw asyncio.create_task/asyncio.sleep (not anyio's portable
    equivalents), which would fail for the wrong reason if trio ever became
    available transitively.
    """
    return "asyncio"
