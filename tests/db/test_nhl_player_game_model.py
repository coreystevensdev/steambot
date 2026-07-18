"""Model-shape test for NhlPlayerGame: confirms columns exist and round-trips through sqlite."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, NhlPlayerGame


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_nhl_player_game_round_trips(session_factory):
    row = NhlPlayerGame(
        season=2025,
        game_date=datetime(2025, 12, 1, tzinfo=timezone.utc),
        player="Connor McDavid",
        team="Edmonton Oilers",
        opponent="Calgary Flames",
        opposing_goalie="Dustin Wolf",
        is_home=True,
        rest_days=2,
        goals=1,
        assists=2,
        points=3,
        shots_on_goal=5,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()

    async with session_factory() as session:
        saved = (await session.execute(select(NhlPlayerGame))).scalars().first()
    assert saved.player == "Connor McDavid"
    assert saved.is_home is True
    assert saved.points == 3
    assert saved.rest_days == 2
