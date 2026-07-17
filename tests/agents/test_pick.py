"""Tests for pick_agent's sim matching. The Claude call itself is not tested
here; matching and blending are pure and run before/after it."""

from __future__ import annotations

from datetime import datetime, timezone

from fairline.agents.pick import _format_steam, _format_team_stats, _sim_probability
from fairline.state import GameSnapshot, SimLine

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


class TestFormatTeamStats:
    def test_formats_up_to_six_numeric_fields_sorted_by_key(self):
        stats = {
            "Kansas City Chiefs": {
                "points": 420, "turnovers": 12, "yards": 5900,
                "sacks": 45, "takeaways": 20, "penalties": 88, "wins": 14,
            }
        }
        text = _format_team_stats(stats, "Kansas City Chiefs")
        assert text.startswith("Kansas City Chiefs: ")
        assert text.count("=") == 6

    def test_empty_for_unknown_team(self):
        assert _format_team_stats({}, "Kansas City Chiefs") == ""

    def test_skips_non_numeric_fields(self):
        stats = {"Kansas City Chiefs": {"team": {"id": 1}, "points": 420}}
        assert _format_team_stats(stats, "Kansas City Chiefs") == "Kansas City Chiefs: points=420"


class TestFormatSteam:
    def test_formats_events_for_a_game(self):
        signal = {"game-1": ["STEAM Kansas City Chiefs (h2h) -110 -> -125 prob +0.033 in 6m via pinnacle"]}
        text = _format_steam(signal, "game-1")
        assert text == "\n  Steam moves: STEAM Kansas City Chiefs (h2h) -110 -> -125 prob +0.033 in 6m via pinnacle"

    def test_empty_for_unknown_game(self):
        assert _format_steam({}, "game-1") == ""


def test_build_prompt_includes_stats_and_steam_sections():
    from fairline.agents.pick import _build_prompt
    from fairline.state import FairLine

    game = GAME
    fair_line = FairLine(
        game_id="game-1", market="h2h", outcomes=["Kansas City Chiefs", "Las Vegas Raiders"],
        fair_probs=[0.58, 0.42], source_book="pinnacle",
    )
    prompt = _build_prompt(
        [game], [fair_line],
        team_stats={"Kansas City Chiefs": {"points": 420}},
        steam_signal={"game-1": ["STEAM Kansas City Chiefs (h2h) -110 -> -125 prob +0.033 in 6m via pinnacle"]},
    )
    assert "points=420" in prompt
    assert "Steam moves: STEAM" in prompt
