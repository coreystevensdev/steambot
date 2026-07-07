"""Tests for line-history capture: window filtering, snapshot flattening, storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from steambot.db.models import Base, LineSnapshot
from steambot.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome
from steambot.steam import games_in_window, record_snapshots, snapshot_rows

NOW = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _game(game_id: str = "game-1", kickoff: datetime | None = None) -> GameSnapshot:
    return GameSnapshot(
        game_id=game_id,
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=kickoff or (NOW + timedelta(hours=2)),
        bookmakers=[
            BookmakerOdds(
                key="pinnacle",
                title="Pinnacle",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=-118, point=-3.5),
                            Outcome(name="Las Vegas Raiders", price=-102, point=3.5),
                        ],
                    )
                ],
            ),
            BookmakerOdds(
                key="draftkings",
                title="DraftKings",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=-110, point=-3.5),
                            Outcome(name="Las Vegas Raiders", price=-110, point=3.5),
                        ],
                    )
                ],
            ),
            BookmakerOdds(
                key="unibet",  # not in the tracked book set
                title="Unibet",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[Outcome(name="Kansas City Chiefs", price=-109, point=-3.5)],
                    )
                ],
            ),
        ],
    )


def test_window_keeps_upcoming_games_only():
    soon = _game("g-soon", NOW + timedelta(hours=2))
    started = _game("g-started", NOW - timedelta(minutes=5))
    far = _game("g-far", NOW + timedelta(hours=30))

    kept = games_in_window([soon, started, far], now=NOW, window_hours=3)

    assert [g.game_id for g in kept] == ["g-soon"]


def test_snapshot_rows_flattens_tracked_books_only():
    rows = snapshot_rows([_game()], captured_at=NOW)

    books = {r.book for r in rows}
    assert books == {"pinnacle", "draftkings"}
    assert len(rows) == 4  # 2 books x 2 outcomes
    pin = next(r for r in rows if r.book == "pinnacle" and r.outcome == "Kansas City Chiefs")
    assert pin.price == -118
    assert pin.point == -3.5
    assert pin.market == "spreads"
    assert pin.captured_at == NOW


async def test_record_snapshots_persists_rows(session_factory):
    written = await record_snapshots([_game()], session_factory, captured_at=NOW)

    assert written == 4
    async with session_factory() as session:
        rows = (await session.execute(select(LineSnapshot))).scalars().all()
    assert len(rows) == 4
    assert {r.game_id for r in rows} == {"game-1"}
