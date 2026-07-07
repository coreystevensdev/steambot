"""Tests for the trends engine: game-result capture and ATS/O-U/SU records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, GameResult, LineSnapshot
from fairline.state import GameScore
from fairline.trends import compute_team_trends, record_game_results

NOW = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _result(i: int, home_score: int, away_score: int, spread_home: float | None = -3.5, total: float | None = 44.5) -> GameResult:
    return GameResult(
        game_id=f"g{i}",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=NOW - timedelta(days=7 * i),
        home_score=home_score,
        away_score=away_score,
        closing_spread_home=spread_home,
        closing_total=total,
    )


class TestComputeTeamTrends:
    def test_ats_from_home_perspective(self):
        # won by 7 vs -3.5 (cover), won by 3 vs -3.5 (no cover)
        results = [_result(1, 27, 20), _result(2, 23, 20)]

        t = compute_team_trends(results, "Kansas City Chiefs", last_n=10)

        assert t["su"] == "2-0-0"
        assert t["ats"] == "1-1-0"
        assert t["n"] == 2

    def test_ats_flips_sign_for_the_away_team(self):
        # Raiders +3.5 away: lost by 3 covers, lost by 7 does not
        results = [_result(1, 23, 20), _result(2, 27, 20)]

        t = compute_team_trends(results, "Las Vegas Raiders", last_n=10)

        assert t["su"] == "0-2-0"
        assert t["ats"] == "1-1-0"

    def test_over_under_and_pushes(self):
        # totals 44.5: 47 over, 40 under; spread -3.0 with 3-point win pushes
        results = [_result(1, 27, 20, spread_home=-3.0), _result(2, 20, 20, total=40.0)]

        t = compute_team_trends(results, "Kansas City Chiefs", last_n=10)

        assert t["ou"] == "1-0-1"
        # game 1: margin 7 + (-3.0) > 0 covers; game 2: margin 0 + (-3.5) < 0 does not
        assert t["ats"] == "1-1-0"

    def test_last_n_limits_the_window(self):
        results = [_result(i, 27, 20) for i in range(1, 6)]

        t = compute_team_trends(results, "Kansas City Chiefs", last_n=3)

        assert t["n"] == 3
        assert t["su"] == "3-0-0"

    def test_games_without_lines_count_su_only(self):
        results = [_result(1, 27, 20, spread_home=None, total=None)]

        t = compute_team_trends(results, "Kansas City Chiefs", last_n=10)

        assert t["su"] == "1-0-0"
        assert t["ats"] == "0-0-0"
        assert t["ou"] == "0-0-0"


def _snapshot(market: str, outcome: str, point: float | None, captured_at: datetime) -> LineSnapshot:
    return LineSnapshot(
        game_id="g1",
        sport="americanfootball_nfl",
        book="pinnacle",
        market=market,
        outcome=outcome,
        price=-110,
        point=point,
        captured_at=captured_at,
    )


async def test_record_game_results_joins_scores_with_closing_lines(session_factory):
    async with session_factory() as session:
        # two cycles; the later one is the closing approximation
        session.add(_snapshot("spreads", "Kansas City Chiefs", -3.0, NOW - timedelta(minutes=30)))
        session.add(_snapshot("spreads", "Kansas City Chiefs", -3.5, NOW - timedelta(minutes=5)))
        session.add(_snapshot("totals", "Over", 44.5, NOW - timedelta(minutes=5)))
        await session.commit()

    score = GameScore(
        game_id="g1",
        completed=True,
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        home_score=27,
        away_score=20,
        commence_time=NOW,
    )
    written = await record_game_results([score], session_factory, sport="americanfootball_nfl")

    assert written == 1
    async with session_factory() as session:
        row = (await session.execute(select(GameResult))).scalars().one()
    assert row.home_score == 27
    assert row.closing_spread_home == -3.5
    assert row.closing_total == 44.5


async def test_record_game_results_is_idempotent(session_factory):
    score = GameScore(
        game_id="g1",
        completed=True,
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        home_score=27,
        away_score=20,
        commence_time=NOW,
    )
    await record_game_results([score], session_factory, sport="americanfootball_nfl")
    await record_game_results([score], session_factory, sport="americanfootball_nfl")

    async with session_factory() as session:
        rows = (await session.execute(select(GameResult))).scalars().all()
    assert len(rows) == 1


async def test_record_skips_incomplete_games(session_factory):
    score = GameScore(
        game_id="g1",
        completed=False,
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
    )
    written = await record_game_results([score], session_factory, sport="americanfootball_nfl")

    assert written == 0


async def test_trends_agent_attaches_records_for_slate_teams(session_factory):
    from fairline.state import BookmakerOdds, GameSnapshot
    from fairline.trends import trends_agent

    async with session_factory() as session:
        session.add(_result(1, 27, 20))
        session.add(_result(2, 23, 20))
        await session.commit()

    game = GameSnapshot(
        game_id="upcoming-1",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=NOW + timedelta(days=1),
        bookmakers=[],
    )
    state = {"sport": "americanfootball_nfl", "games": [game]}

    out = await trends_agent(state, session_factory=session_factory)

    trends = out["team_trends"]
    assert trends["Kansas City Chiefs"]["su"] == "2-0-0"
    assert trends["Las Vegas Raiders"]["su"] == "0-2-0"


async def test_trends_agent_without_db_returns_empty():
    from fairline.trends import trends_agent

    out = await trends_agent({"games": []}, session_factory=None)
    assert out == {"team_trends": {}}
