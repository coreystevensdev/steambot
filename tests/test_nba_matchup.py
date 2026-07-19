"""Tests for NBA splits computation: last-N, season, home/away, back-to-back.
No vs-defender split exists for NBA (no verified per-game defender-matchup
data source), so there is no sample-size-floor test in this module, unlike
the MLB/NHL equivalents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fairline.db.models import NbaPlayerGame
from fairline.nba_matchup import (
    NBA_PROP_STAT_COLUMNS,
    compute_nba_prop_splits,
    describe_nba_splits,
    nba_matchup_probability,
)

BASE_DATE = datetime(2025, 12, 1, tzinfo=timezone.utc)


def _game(points=25, is_home=True, rest_days=2, season=2025, day=0):
    return NbaPlayerGame(
        season=season, game_date=BASE_DATE + timedelta(days=day), player="LeBron James",
        team="Los Angeles Lakers", opponent="Boston Celtics",
        is_home=is_home, rest_days=rest_days,
        points=points, rebounds=8, assists=9, three_pointers_made=3,
    )


class TestComputeNbaPropSplits:
    def test_home_and_away_splits_present(self):
        games = [_game(is_home=True, day=0), _game(is_home=False, day=1, points=15)]
        splits = compute_nba_prop_splits(games, "points", 20.5)
        assert splits["home"] == (1, 1)
        assert splits["away"] == (0, 1)

    def test_back_to_back_split(self):
        games = [_game(rest_days=1, day=0), _game(rest_days=3, day=1, points=15)]
        splits = compute_nba_prop_splits(games, "points", 20.5)
        assert splits["back_to_back"] == (1, 1)

    def test_no_vs_defender_key_exists(self):
        games = [_game(day=0)]
        splits = compute_nba_prop_splits(games, "points", 20.5)
        assert "vs_defender" not in splits
        assert set(splits.keys()) == {"last_5", "last_10", "season", "home", "away", "back_to_back"}


def test_nba_prop_stat_columns_covers_four_markets():
    assert NBA_PROP_STAT_COLUMNS == {
        "player_points": "points",
        "player_rebounds": "rebounds",
        "player_assists": "assists",
        "player_threes": "three_pointers_made",
    }


def test_nba_matchup_probability_bounded_near_market():
    games = [_game(day=i) for i in range(10)]
    prob, splits = nba_matchup_probability(games, "points", 20.5, "Over", market_fair=0.55)
    assert 0.49 <= prob <= 0.61 + 1e-9


def test_describe_nba_splits_lists_only_present_splits():
    splits = {"home": (3, 5), "away": (0, 0)}
    text = describe_nba_splits(splits, "Over", 20.5)
    assert "home 3-2 over 20.5" in text
    assert "away" not in text
