"""Tests for pick_agent's sim matching. The Claude call itself is not tested
here; matching and blending are pure and run before/after it."""

from __future__ import annotations

from datetime import datetime, timezone

from steambot.agents.pick import _sim_probability
from steambot.state import GameSnapshot, SimLine

GAME = GameSnapshot(
    game_id="game-1",
    sport="americanfootball_nfl",
    home_team="Kansas City Chiefs",
    away_team="Las Vegas Raiders",
    commence_time=datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc),
    bookmakers=[],
)


def _sim(**overrides) -> SimLine:
    fields = dict(
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        market="spreads",
        selection="Kansas City Chiefs -3.5",
        probability=0.58,
    )
    fields.update(overrides)
    return SimLine(**fields)


def test_sim_matches_by_teams_market_and_selection():
    prob = _sim_probability([_sim()], GAME, "spreads", "Kansas City Chiefs")
    assert prob == 0.58


def test_sim_ignores_other_games():
    other = _sim(home_team="Buffalo Bills", away_team="Miami Dolphins")
    assert _sim_probability([other], GAME, "spreads", "Kansas City Chiefs") is None


def test_sim_ignores_other_markets():
    assert _sim_probability([_sim(market="totals", selection="Over 47.5")], GAME, "spreads", "Kansas City Chiefs") is None


def test_no_sims_returns_none():
    assert _sim_probability([], GAME, "spreads", "Kansas City Chiefs") is None
