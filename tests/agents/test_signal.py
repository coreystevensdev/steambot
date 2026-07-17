"""Tests for signal_agent: wraps the existing steam-move detector for pick_agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.agents.signal import signal_agent
from fairline.db.models import Base, LineSnapshot

NOW = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _rows(captured_at, price_a, price_b):
    common = dict(game_id="game-1", sport="americanfootball_nfl", book="pinnacle", market="h2h", captured_at=captured_at)
    return [
        LineSnapshot(outcome="Kansas City Chiefs", price=price_a, point=None, **common),
        LineSnapshot(outcome="Las Vegas Raiders", price=price_b, point=None, **common),
    ]


@pytest.mark.asyncio
async def test_signal_agent_reports_a_detected_steam_move(session_factory):
    async with session_factory() as session:
        session.add_all(_rows(NOW - timedelta(minutes=8), -110, -110))
        session.add_all(_rows(NOW, -125, 105))
        await session.commit()

    out = await signal_agent({}, session_factory=session_factory)

    assert "game-1" in out["steam_signal"]
    assert "STEAM" in out["steam_signal"]["game-1"][0]


@pytest.mark.asyncio
async def test_signal_agent_empty_when_no_steam(session_factory):
    out = await signal_agent({}, session_factory=session_factory)
    assert out == {"steam_signal": {}}


@pytest.mark.asyncio
async def test_signal_agent_returns_empty_without_session_factory():
    out = await signal_agent({}, session_factory=None)
    assert out == {"steam_signal": {}}
