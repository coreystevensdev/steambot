"""Tests for the CLV settlement job.

Closing line capture: match a stored pick to the sharp book's final pre-game
market, devig it, and compare against the implied probability of the price
the pick was taken at.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from steambot.clv import closing_line_for_selection, settle_closing_lines
from steambot.db.models import Base, Pick
from steambot.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome, american_to_prob

KICKOFF = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _game(price_chiefs: int = -120, price_raiders: int = 100) -> GameSnapshot:
    return GameSnapshot(
        game_id="game-1",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=KICKOFF,
        bookmakers=[
            BookmakerOdds(
                key="pinnacle",
                title="Pinnacle",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=price_chiefs, point=-3.5),
                            Outcome(name="Las Vegas Raiders", price=price_raiders, point=3.5),
                        ],
                    )
                ],
            )
        ],
    )


def _pick(**overrides) -> Pick:
    fields = dict(
        id="pick-1",
        user_id="user-1",
        run_id="run-1",
        game_id="game-1",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=KICKOFF,
        market="spreads",
        selection="Kansas City Chiefs -3.5",
        book="draftkings",
        price=-108,
        sharp_probability=0.545,
        blended_probability=0.545,
        edge_pct=0.026,
        ev_pct=0.031,
        confidence="medium",
        rationale="Sharp line moved against public action.",
        approved_at=KICKOFF - timedelta(hours=2),
    )
    fields.update(overrides)
    return Pick(**fields)


def test_closing_line_devigs_the_selection_side():
    line = closing_line_for_selection(_game(), "spreads", "Kansas City Chiefs -3.5")

    assert line is not None
    assert line.price == -120
    # -120 implies 0.5455, +100 implies 0.5; no-vig share of the Chiefs side
    expected = (120 / 220) / (120 / 220 + 0.5)
    assert line.probability == pytest.approx(expected)


def test_closing_line_missing_market_returns_none():
    game = _game()
    assert closing_line_for_selection(game, "totals", "Over 47.5") is None


def test_closing_line_unmatched_selection_returns_none():
    game = _game()
    assert closing_line_for_selection(game, "spreads", "Denver Broncos -3.5") is None


async def test_settle_writes_closing_fields_and_clv(session_factory):
    async with session_factory() as session:
        session.add(_pick())
        await session.commit()

    summary = await settle_closing_lines(
        [_game()], session_factory, now=KICKOFF - timedelta(minutes=10)
    )

    assert summary == {"settled": 1, "missed": 0, "pending": 0}
    async with session_factory() as session:
        pick = (await session.execute(select(Pick))).scalars().one()
    assert pick.closing_price == -120
    expected_prob = (120 / 220) / (120 / 220 + 0.5)
    assert pick.closing_probability == pytest.approx(expected_prob)
    assert pick.clv == pytest.approx(expected_prob - american_to_prob(-108))


async def test_settle_skips_games_outside_window(session_factory):
    async with session_factory() as session:
        session.add(_pick())
        await session.commit()

    summary = await settle_closing_lines(
        [_game()], session_factory, now=KICKOFF - timedelta(hours=6)
    )

    assert summary == {"settled": 0, "missed": 0, "pending": 1}
    async with session_factory() as session:
        pick = (await session.execute(select(Pick))).scalars().one()
    assert pick.closing_price is None


async def test_settle_counts_game_absent_from_feed_as_missed(session_factory):
    async with session_factory() as session:
        session.add(_pick(game_id="game-gone"))
        await session.commit()

    summary = await settle_closing_lines(
        [_game()], session_factory, now=KICKOFF - timedelta(minutes=10)
    )

    assert summary == {"settled": 0, "missed": 1, "pending": 0}


async def test_settle_ignores_already_settled_picks(session_factory):
    async with session_factory() as session:
        session.add(_pick(closing_price=-125, closing_probability=0.53, clv=0.01))
        await session.commit()

    summary = await settle_closing_lines(
        [_game()], session_factory, now=KICKOFF - timedelta(minutes=10)
    )

    assert summary == {"settled": 0, "missed": 0, "pending": 0}
