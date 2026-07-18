"""Round-trip test for PlayerGame's new context columns."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, PlayerGame


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_player_game_context_columns_round_trip(session_factory):
    row = PlayerGame(
        sport="americanfootball_nfl", season=2025, week=10, player="Patrick Mahomes",
        team="Kansas City Chiefs", opponent="Buffalo Bills",
        is_home=True, surface="grass", is_primetime=True, bad_weather=False,
        passing_yards=310.0,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()

    async with session_factory() as session:
        saved = (await session.execute(select(PlayerGame))).scalars().first()
    assert saved.is_home is True
    assert saved.surface == "grass"
    assert saved.is_primetime is True
    assert saved.bad_weather is False


async def test_player_game_context_columns_default_to_null(session_factory):
    row = PlayerGame(
        sport="americanfootball_nfl", season=2025, week=10, player="Old Backfill Row",
        team="Kansas City Chiefs", opponent="Buffalo Bills",
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()

    async with session_factory() as session:
        saved = (await session.execute(select(PlayerGame))).scalars().first()
    assert saved.is_home is None
    assert saved.surface is None
    assert saved.is_primetime is None
    assert saved.bad_weather is None
