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

from steambot.clv import (
    closing_line_for_selection,
    grade_pick,
    grade_results,
    settle_closing_lines,
)
from steambot.db.models import Base, Pick
from steambot.state import (
    BookmakerOdds,
    GameScore,
    GameSnapshot,
    MarketOdds,
    Outcome,
    american_to_prob,
)

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
    assert line.point == -3.5
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
    assert pick.closing_point == -3.5
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


def _score(home: int = 27, away: int = 20, completed: bool = True) -> GameScore:
    return GameScore(
        game_id="game-1",
        completed=completed,
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        home_score=home,
        away_score=away,
    )


class TestGradePick:
    def test_spread_cover_wins(self):
        # Chiefs -3.5, won by 7
        result = grade_pick("spreads", "Kansas City Chiefs -3.5", -108, _score(27, 20))
        assert result == ("win", pytest.approx(100 / 108))

    def test_spread_failed_cover_loses(self):
        # Chiefs -3.5, won by 3
        result = grade_pick("spreads", "Kansas City Chiefs -3.5", -108, _score(23, 20))
        assert result == ("loss", -1.0)

    def test_spread_exact_margin_pushes(self):
        result = grade_pick("spreads", "Kansas City Chiefs -3.0", -110, _score(23, 20))
        assert result == ("push", 0.0)

    def test_underdog_plus_points_wins_on_close_loss(self):
        result = grade_pick("spreads", "Las Vegas Raiders +3.5", 105, _score(23, 20))
        assert result == ("win", pytest.approx(1.05))

    def test_total_over_wins(self):
        result = grade_pick("totals", "Over 44.5", -110, _score(27, 20))
        assert result == ("win", pytest.approx(100 / 110))

    def test_total_under_loses(self):
        result = grade_pick("totals", "Under 44.5", -110, _score(27, 20))
        assert result == ("loss", -1.0)

    def test_total_on_the_number_pushes(self):
        result = grade_pick("totals", "Over 47.0", -110, _score(27, 20))
        assert result == ("push", 0.0)

    def test_h2h_winner(self):
        result = grade_pick("h2h", "Kansas City Chiefs", -165, _score(27, 20))
        assert result == ("win", pytest.approx(100 / 165))

    def test_h2h_tie_pushes(self):
        result = grade_pick("h2h", "Kansas City Chiefs", -165, _score(20, 20))
        assert result == ("push", 0.0)

    def test_unknown_team_returns_none(self):
        assert grade_pick("h2h", "Denver Broncos", -110, _score()) is None

    def test_malformed_spread_selection_returns_none(self):
        assert grade_pick("spreads", "Kansas City Chiefs", -110, _score()) is None


async def test_grade_results_writes_result_and_profit(session_factory):
    async with session_factory() as session:
        session.add(_pick())
        await session.commit()

    summary = await grade_results([_score(27, 20)], session_factory)

    assert summary == {"graded": 1, "pending": 0, "missed": 0}
    async with session_factory() as session:
        pick = (await session.execute(select(Pick))).scalars().one()
    assert pick.result == "win"
    assert pick.profit_units == pytest.approx(100 / 108)


async def test_grade_results_incomplete_game_is_pending(session_factory):
    async with session_factory() as session:
        session.add(_pick())
        await session.commit()

    summary = await grade_results([_score(completed=False)], session_factory)

    assert summary == {"graded": 0, "pending": 1, "missed": 0}


async def test_grade_results_absent_game_is_missed(session_factory):
    async with session_factory() as session:
        session.add(_pick(game_id="game-gone"))
        await session.commit()

    summary = await grade_results([_score()], session_factory)

    assert summary == {"graded": 0, "pending": 0, "missed": 1}


async def test_grade_results_skips_already_graded(session_factory):
    async with session_factory() as session:
        session.add(_pick(result="loss", profit_units=-1.0))
        await session.commit()

    summary = await grade_results([_score()], session_factory)

    assert summary == {"graded": 0, "pending": 0, "missed": 0}


def test_closing_line_h2h_has_no_point():
    game = GameSnapshot(
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
                        key="h2h",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=-165),
                            Outcome(name="Las Vegas Raiders", price=148),
                        ],
                    )
                ],
            )
        ],
    )
    line = closing_line_for_selection(game, "h2h", "Kansas City Chiefs")
    assert line is not None
    assert line.point is None


async def test_sim_clv_report_splits_agree_and_disagree(session_factory):
    from steambot.clv import sim_clv_report

    async with session_factory() as session:
        # sim agreed with the sharp line (|sim - sharp| < threshold), beat the close
        session.add(_pick(id="p-agree", sim_probability=0.55, clv=0.010, result="win"))
        # sim disagreed hard, lost to the close
        session.add(_pick(id="p-disagree", sim_probability=0.62, clv=-0.005, result="loss"))
        # never settled -> excluded
        session.add(_pick(id="p-unsettled", sim_probability=0.60))
        # no sim supplied -> its own bucket
        session.add(_pick(id="p-nosim", clv=0.002, result="win"))
        await session.commit()

    report = await sim_clv_report(session_factory, disagree_threshold=0.02)

    assert report["agreed"]["count"] == 1
    assert report["agreed"]["avg_clv"] == pytest.approx(0.010)
    assert report["disagreed"]["count"] == 1
    assert report["disagreed"]["avg_clv"] == pytest.approx(-0.005)
    assert report["no_sim"]["count"] == 1
