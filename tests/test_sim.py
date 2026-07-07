"""Tests for the ratings-based simulation model and its agent node."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, GameResult
from fairline.sim import (
    HFA_POINTS,
    SIGMA_MARGIN,
    build_ratings,
    cover_probability,
    parse_nflverse_games,
    season_of,
    sim_agent,
    win_probability,
)
from fairline.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome, SimLine

NOW = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class TestMarginModel:
    def test_equal_teams_at_home_win_about_56_percent(self):
        # design doc pin: expected margin = HFA, sigma 13.5
        p = win_probability(HFA_POINTS)
        assert p == pytest.approx(0.5589, abs=0.002)

    def test_zero_margin_is_a_coin_flip(self):
        assert win_probability(0.0) == pytest.approx(0.5)

    def test_cover_probability_laying_points(self):
        # 2-point better team laying 3.5: P(margin > 3.5) = phi((2 - 3.5) / sigma)
        p = cover_probability(expected_margin=2.0, team_point=-3.5)
        assert p == pytest.approx(0.4558, abs=0.002)

    def test_cover_probability_getting_points(self):
        p = cover_probability(expected_margin=-2.0, team_point=3.5)
        assert p == pytest.approx(0.5442, abs=0.002)


class TestRatings:
    def test_winner_gains_and_loser_drops(self):
        games = [
            {"season": 2025, "home_team": "A", "away_team": "B", "home_score": 30, "away_score": 10},
        ]
        ratings = build_ratings(games)
        assert ratings["A"] > 0 > ratings["B"]

    def test_repeated_blowouts_converge_toward_the_margin(self):
        games = [
            {"season": 2025, "home_team": "A", "away_team": "B", "home_score": 24, "away_score": 10}
            for _ in range(60)
        ]
        ratings = build_ratings(games)
        # A beats B by 14 at home; rating gap should approach 14 - HFA = 12
        assert ratings["A"] - ratings["B"] == pytest.approx(12.0, abs=2.0)

    def test_season_boundary_regresses_toward_zero(self):
        one_season = build_ratings(
            [{"season": 2024, "home_team": "A", "away_team": "B", "home_score": 30, "away_score": 10}]
        )
        crossed = build_ratings(
            [
                {"season": 2024, "home_team": "A", "away_team": "B", "home_score": 30, "away_score": 10},
                {"season": 2025, "home_team": "C", "away_team": "D", "home_score": 20, "away_score": 17},
            ]
        )
        assert abs(crossed["A"]) < abs(one_season["A"])


def test_season_of_maps_january_to_prior_season():
    assert season_of(datetime(2026, 1, 15, tzinfo=timezone.utc)) == 2025
    assert season_of(datetime(2025, 10, 5, tzinfo=timezone.utc)) == 2025


def test_parse_nflverse_games_maps_codes_and_lines():
    csv_text = (
        "game_id,season,week,gameday,home_team,away_team,home_score,away_score,spread_line,total_line\n"
        "2025_10_LV_KC,2025,10,2025-11-09,KC,LV,27,20,3.5,44.5\n"
        "2025_11_KC_DEN,2025,11,2025-11-16,DEN,KC,,,2.5,41.0\n"
    )
    sim_games, results = parse_nflverse_games(csv_text)

    assert len(sim_games) == 1  # unscored future game skipped
    g = sim_games[0]
    assert g["home_team"] == "Kansas City Chiefs"
    assert g["away_team"] == "Las Vegas Raiders"

    assert len(results) == 1
    r = results[0]
    assert r.game_id == "2025_10_LV_KC"
    # spread_line 3.5 = home favored by 3.5 -> home handicap -3.5
    assert r.closing_spread_home == -3.5
    assert r.closing_total == 44.5


async def test_sim_agent_writes_h2h_and_spread_lines(session_factory):
    async with session_factory() as session:
        for i in range(1, 9):
            session.add(
                GameResult(
                    game_id=f"h{i}",
                    sport="americanfootball_nfl",
                    home_team="Kansas City Chiefs",
                    away_team="Denver Broncos",
                    commence_time=NOW - timedelta(days=7 * i),
                    home_score=30,
                    away_score=13,
                )
            )
        await session.commit()

    game = GameSnapshot(
        game_id="up-1",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Denver Broncos",
        commence_time=NOW + timedelta(days=2),
        bookmakers=[
            BookmakerOdds(
                key="pinnacle",
                title="Pinnacle",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=-110, point=-7.5),
                            Outcome(name="Denver Broncos", price=-110, point=7.5),
                        ],
                    )
                ],
            )
        ],
    )
    state = {"sport": "americanfootball_nfl", "games": [game], "sim_lines": []}

    out = await sim_agent(state, session_factory=session_factory)

    lines = out["sim_lines"]
    h2h = [sl for sl in lines if sl.market == "h2h" and sl.selection == "Kansas City Chiefs"]
    spreads = [sl for sl in lines if sl.market == "spreads" and sl.selection.startswith("Kansas City Chiefs")]
    assert len(h2h) == 1 and h2h[0].probability > 0.6
    assert len(spreads) == 1 and 0.0 < spreads[0].probability < 1.0


async def test_sim_agent_defers_to_caller_supplied_lines(session_factory):
    async with session_factory() as session:
        session.add(
            GameResult(
                game_id="h1",
                sport="americanfootball_nfl",
                home_team="Kansas City Chiefs",
                away_team="Denver Broncos",
                commence_time=NOW - timedelta(days=7),
                home_score=30,
                away_score=13,
            )
        )
        await session.commit()

    caller_line = SimLine(
        home_team="Kansas City Chiefs",
        away_team="Denver Broncos",
        market="h2h",
        selection="Kansas City Chiefs",
        probability=0.99,
    )
    game = GameSnapshot(
        game_id="up-1",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Denver Broncos",
        commence_time=NOW + timedelta(days=2),
        bookmakers=[],
    )
    state = {"sport": "americanfootball_nfl", "games": [game], "sim_lines": [caller_line]}

    out = await sim_agent(state, session_factory=session_factory)

    h2h = [sl for sl in out["sim_lines"] if sl.market == "h2h"]
    assert len(h2h) == 1
    assert h2h[0].probability == 0.99


async def test_sim_agent_without_db_passes_caller_lines_through():
    line = SimLine(
        home_team="A", away_team="B", market="h2h", selection="A", probability=0.6
    )
    out = await sim_agent({"games": [], "sim_lines": [line]}, session_factory=None)
    assert out == {"sim_lines": [line]}
