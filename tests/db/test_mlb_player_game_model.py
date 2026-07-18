"""Model-shape test for MlbPlayerGame: confirms columns exist and round-trips through sqlite."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, MlbPlayerGame


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_mlb_player_game_round_trips(session_factory):
    row = MlbPlayerGame(
        season=2025,
        game_date=datetime(2025, 6, 14, 19, 5, tzinfo=timezone.utc),
        player="Aaron Judge",
        team="New York Yankees",
        opponent="Boston Red Sox",
        opposing_pitcher="Brayan Bello",
        is_home=True,
        day_night="night",
        at_bats=4,
        hits=2,
        home_runs=1,
        rbis=3,
        total_bases=6,
        strikeouts=1,
        walks=0,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()

    async with session_factory() as session:
        result = (await session.execute(select(MlbPlayerGame))).scalars().all()
    assert len(result) == 1
    saved = result[0]
    assert saved.player == "Aaron Judge"
    assert saved.is_home is True
    assert saved.day_night == "night"
    assert saved.home_runs == 1
