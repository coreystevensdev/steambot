"""Tests for MLB Statcast aggregation: per-game batter totals, starter derivation,
day/night and home/away context. Uses a small literal DataFrame fixture, never a
live pybaseball call."""

from __future__ import annotations

import pandas as pd

from fairline.mlb_stats import _aggregate_batter_games, _derive_starters, _lookup_schedule_context

# One game (game_pk=1), Yankees at home vs Red Sox. Judge goes 2-for-4 with a
# home run (1 (single) + 4 (home run) = 5 total bases), 1 strikeout, faces
# starter Brayan Bello the whole game.
_STATCAST_FIXTURE = pd.DataFrame(
    [
        {"game_pk": 1, "game_date": "2025-06-14", "home_team": "NYY", "away_team": "BOS",
         "batter": 592450, "pitcher": 700242, "events": "single",
         "bat_score": 0, "post_bat_score": 0, "inning": 1, "inning_topbot": "Bot"},
        {"game_pk": 1, "game_date": "2025-06-14", "home_team": "NYY", "away_team": "BOS",
         "batter": 592450, "pitcher": 700242, "events": "strikeout",
         "bat_score": 0, "post_bat_score": 0, "inning": 3, "inning_topbot": "Bot"},
        {"game_pk": 1, "game_date": "2025-06-14", "home_team": "NYY", "away_team": "BOS",
         "batter": 592450, "pitcher": 700242, "events": "home_run",
         "bat_score": 0, "post_bat_score": 3, "inning": 5, "inning_topbot": "Bot"},
        {"game_pk": 1, "game_date": "2025-06-14", "home_team": "NYY", "away_team": "BOS",
         "batter": 592450, "pitcher": 700242, "events": "field_out",
         "bat_score": 3, "post_bat_score": 3, "inning": 7, "inning_topbot": "Bot"},
        # a non-batter-outcome row (ball/strike, no terminal event) must be ignored for AB/hit counting
        {"game_pk": 1, "game_date": "2025-06-14", "home_team": "NYY", "away_team": "BOS",
         "batter": 592450, "pitcher": 700242, "events": float("nan"),
         "bat_score": 3, "post_bat_score": 3, "inning": 7, "inning_topbot": "Bot"},
    ]
)

_TEAM_NAMES = {"NYY": "New York Yankees", "BOS": "Boston Red Sox"}
_STARTER_NAMES = {700242: "Brayan Bello"}
_BATTER_NAMES = {592450: "Aaron Judge"}


class TestAggregateBatterGames:
    def test_counts_ab_hits_hr_rbi_total_bases_k_bb(self):
        rows = _aggregate_batter_games(
            _STATCAST_FIXTURE, team_names=_TEAM_NAMES, player_names=_BATTER_NAMES,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["at_bats"] == 4  # single, strikeout, home_run, field_out all count as AB (2-for-4)
        assert row["hits"] == 2  # single + home_run
        assert row["home_runs"] == 1
        assert row["total_bases"] == 5  # 1 (single) + 4 (home run)
        assert row["strikeouts"] == 1
        assert row["walks"] == 0
        assert row["rbis"] == 3  # post_bat_score - bat_score on the home_run row
        assert row["team"] == "New York Yankees"
        assert row["opponent"] == "Boston Red Sox"
        assert row["is_home"] is True
        assert row["player"] == "Aaron Judge"

    def test_walk_does_not_count_as_at_bat(self):
        walk_df = pd.DataFrame(
            [{"game_pk": 2, "game_date": "2025-06-15", "home_team": "BOS", "away_team": "NYY",
              "batter": 592450, "pitcher": 700242, "events": "walk",
              "bat_score": 0, "post_bat_score": 0, "inning": 1, "inning_topbot": "Top"}]
        )
        rows = _aggregate_batter_games(walk_df, team_names=_TEAM_NAMES, player_names=_BATTER_NAMES)
        assert rows[0]["at_bats"] == 0
        assert rows[0]["walks"] == 1
        assert rows[0]["is_home"] is False  # NYY is the away team in this row

    def test_intent_walk_counts_as_walk_not_at_bat(self):
        # intentional walk is scored the same as a regular walk: a walk, never an AB
        ibb_df = pd.DataFrame(
            [{"game_pk": 3, "game_date": "2025-06-16", "home_team": "BOS", "away_team": "NYY",
              "batter": 592450, "pitcher": 700242, "events": "intent_walk",
              "bat_score": 0, "post_bat_score": 0, "inning": 1, "inning_topbot": "Top"}]
        )
        rows = _aggregate_batter_games(ibb_df, team_names=_TEAM_NAMES, player_names=_BATTER_NAMES)
        assert rows[0]["walks"] == 1
        assert rows[0]["at_bats"] == 0
        assert rows[0]["hits"] == 0

    def test_fielders_choice_out_counts_as_at_bat_not_hit(self):
        # reaching on a fielder's choice where a runner is out is an AB, not a hit
        fco_df = pd.DataFrame(
            [{"game_pk": 4, "game_date": "2025-06-17", "home_team": "BOS", "away_team": "NYY",
              "batter": 592450, "pitcher": 700242, "events": "fielders_choice_out",
              "bat_score": 0, "post_bat_score": 0, "inning": 1, "inning_topbot": "Top"}]
        )
        rows = _aggregate_batter_games(fco_df, team_names=_TEAM_NAMES, player_names=_BATTER_NAMES)
        assert rows[0]["at_bats"] == 1
        assert rows[0]["hits"] == 0
        assert rows[0]["walks"] == 0


class TestDeriveStarters:
    def test_starter_is_pitcher_on_first_pitch_of_each_half_inning_group(self):
        starters = _derive_starters(_STATCAST_FIXTURE, pitcher_names=_STARTER_NAMES)
        assert starters[(1, "Bot")] == "Brayan Bello"


# Real schedule_and_record Date format, verified live against the 2024 NYY
# schedule: "Weekday, Mon D" with no year, and a "(1)"/"(2)" suffix on
# doubleheader dates (e.g. "Saturday, Apr 13 (1)" / "Saturday, Apr 13 (2)").
_DOUBLEHEADER_SCHEDULE = pd.DataFrame(
    [
        {"Date": "Saturday, Apr 13 (1)", "D/N": "D"},
        {"Date": "Saturday, Apr 13 (2)", "D/N": "N"},
        {"Date": "Sunday, Apr 14", "D/N": "D"},
    ]
)


class TestLookupScheduleContext:
    def test_disambiguates_doubleheader_by_game_number(self):
        first_game = _lookup_schedule_context(_DOUBLEHEADER_SCHEDULE, "2024-04-13", game_number=1)
        second_game = _lookup_schedule_context(_DOUBLEHEADER_SCHEDULE, "2024-04-13", game_number=2)
        assert first_game["day_night"] == "day"
        assert second_game["day_night"] == "night"

    def test_single_game_date_ignores_game_number(self):
        context = _lookup_schedule_context(_DOUBLEHEADER_SCHEDULE, "2024-04-14", game_number=1)
        assert context["day_night"] == "day"

    def test_no_matching_date_returns_empty(self):
        assert _lookup_schedule_context(_DOUBLEHEADER_SCHEDULE, "2024-05-01", game_number=1) == {}
