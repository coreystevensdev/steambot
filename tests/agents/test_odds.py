"""Unit tests for odds_agent node logic.

Tests cover best_sharp_book priority ordering and _derive_fair_line math.
These are pure (no network, no graph) so they run without API keys.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from steambot.agents.odds import best_sharp_book, _derive_fair_line
from steambot.state import (
    BookmakerOdds,
    GameSnapshot,
    MarketOdds,
    Outcome,
)


def _make_game(bookmaker_keys: list[str], market_prices: list[int] | None = None) -> GameSnapshot:
    """Helper: build a minimal GameSnapshot with the given bookmaker keys."""
    if market_prices is None:
        market_prices = [-110, -110]
    bookmakers = []
    for key in bookmaker_keys:
        outcomes = [
            Outcome(name="Team A", price=market_prices[0]),
            Outcome(name="Team B", price=market_prices[1]),
        ]
        mkt = MarketOdds(key="spreads", outcomes=outcomes)
        bookmakers.append(BookmakerOdds(key=key, title=key.capitalize(), markets=[mkt]))
    return GameSnapshot(
        game_id="test-game-1",
        sport="americanfootball_nfl",
        home_team="Team A",
        away_team="Team B",
        commence_time=datetime(2026, 1, 15, 20, 0),
        bookmakers=bookmakers,
    )


class TestBestSharpBook:
    def test_pinnacle_wins_over_betonline(self):
        game = _make_game(["betonlineag", "pinnacle", "fanduel"])
        assert best_sharp_book(game) == "pinnacle"

    def test_betonline_wins_when_pinnacle_absent(self):
        game = _make_game(["mybookieag", "betonlineag", "fanduel"])
        assert best_sharp_book(game) == "betonlineag"

    def test_mybookie_last_resort(self):
        game = _make_game(["mybookieag", "draftkings"])
        assert best_sharp_book(game) == "mybookieag"

    def test_no_sharp_book_returns_none(self):
        game = _make_game(["fanduel", "draftkings", "betmgm"])
        assert best_sharp_book(game) is None

    def test_only_pinnacle_present(self):
        game = _make_game(["pinnacle"])
        assert best_sharp_book(game) == "pinnacle"


class TestDeriveFairLine:
    def test_even_spread_produces_fifty_fifty(self):
        game = _make_game(["pinnacle"], market_prices=[-110, -110])
        fl = _derive_fair_line(game, "spreads", "pinnacle")
        assert fl is not None
        assert abs(fl.fair_probs[0] - 0.5) < 0.001
        assert abs(fl.fair_probs[1] - 0.5) < 0.001
        assert abs(sum(fl.fair_probs) - 1.0) < 1e-9

    def test_favorite_gets_higher_fair_probability(self):
        # -200 favorite, +175 dog
        game = _make_game(["pinnacle"], market_prices=[-200, 175])
        fl = _derive_fair_line(game, "spreads", "pinnacle")
        assert fl is not None
        assert fl.fair_probs[0] > fl.fair_probs[1]

    def test_fair_probs_sum_to_one(self):
        game = _make_game(["pinnacle"], market_prices=[-150, 130])
        fl = _derive_fair_line(game, "spreads", "pinnacle")
        assert fl is not None
        assert abs(sum(fl.fair_probs) - 1.0) < 1e-9

    def test_missing_book_returns_none(self):
        game = _make_game(["fanduel"])
        fl = _derive_fair_line(game, "spreads", "pinnacle")
        assert fl is None

    def test_missing_market_returns_none(self):
        game = _make_game(["pinnacle"])
        # Game only has "spreads" market; asking for "h2h" should fail.
        fl = _derive_fair_line(game, "h2h", "pinnacle")
        assert fl is None

    def test_fair_line_metadata(self):
        game = _make_game(["pinnacle"])
        fl = _derive_fair_line(game, "spreads", "pinnacle")
        assert fl is not None
        assert fl.game_id == "test-game-1"
        assert fl.market == "spreads"
        assert fl.source_book == "pinnacle"
        assert fl.outcomes == ["Team A", "Team B"]
