"""Tests for the run registry: Postgres-backed with an in-memory demo fallback."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base
from fairline.runs import _memory_runs, create_run, fetch_run, update_run


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def clear_memory_runs():
    _memory_runs.clear()
    yield
    _memory_runs.clear()


async def test_db_backed_run_lifecycle(session_factory):
    await create_run(session_factory, "r1", "alice")

    run = await fetch_run(session_factory, "r1")
    assert run["user_id"] == "alice"
    assert run["status"] == "running"

    await update_run(session_factory, "r1", "awaiting_review")
    run = await fetch_run(session_factory, "r1")
    assert run["status"] == "awaiting_review"
    assert run["error"] is None

    await update_run(session_factory, "r1", "error", error="boom")
    run = await fetch_run(session_factory, "r1")
    assert run["status"] == "error"
    assert run["error"] == "boom"


async def test_fetch_unknown_run_returns_none(session_factory):
    assert await fetch_run(session_factory, "nope") is None


async def test_memory_fallback_without_db():
    await create_run(None, "r1", "alice")

    run = await fetch_run(None, "r1")
    assert run["user_id"] == "alice"
    assert run["status"] == "running"

    await update_run(None, "r1", "complete")
    assert (await fetch_run(None, "r1"))["status"] == "complete"
    assert await fetch_run(None, "missing") is None


async def test_runs_survive_across_factory_sessions(session_factory):
    """The point of the migration: a second process sees the same runs."""
    await create_run(session_factory, "r1", "alice")

    run = await fetch_run(session_factory, "r1")
    assert run is not None
    assert "r1" not in _memory_runs
