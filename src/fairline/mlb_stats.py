"""MLB batter game logs via pybaseball's Statcast pull, aggregated to the
per-game totals fairline's splits engine needs.

pybaseball has no single call that returns box-score stat lines joined with
game context (home/away, day/night, opposing starter) in one shot. `statcast()`
gives pitch-level rows that this module aggregates up to per-game batter
totals; `schedule_and_record()` gives the home/away and day/night context per
team, joined in by date. Statcast's own rows carry no day/night flag directly
(verified 2026-07-18 against a live pull), which is why that context comes
from the schedule instead.

pybaseball is synchronous and scrapes Baseball Reference/Baseball Savant
under the hood, so every call runs via asyncio.to_thread to avoid blocking
fairline's event loop.
"""

from __future__ import annotations

import logging
import re
from asyncio import to_thread
from datetime import datetime

import pandas as pd

from fairline.db.models import MlbPlayerGame

logger = logging.getLogger(__name__)

# Verified against a live statcast() pull (2025-06-01, 4,215 rows) rather than
# assumed. Real vocab has a couple extra outcomes: intent_walk (a walk) and
# fielders_choice_out (an at-bat, alongside the rarer fielders_choice).
# sac_bunt, catcher_interf, and truncated_pa are left out of every set on
# purpose -- none of them count toward AB, hits, walks, or K in real scoring.
_HIT_EVENTS = {"single", "double", "triple", "home_run"}
_TOTAL_BASES = {"single": 1, "double": 2, "triple": 3, "home_run": 4}
_AT_BAT_EVENTS = _HIT_EVENTS | {
    "strikeout", "field_out", "force_out", "grounded_into_double_play",
    "field_error", "fielders_choice", "fielders_choice_out", "double_play",
}
_WALK_EVENTS = {"walk", "intent_walk", "hit_by_pitch"}
_STRIKEOUT_EVENTS = {"strikeout"}

# Statcast team codes, verified against the same live pull -- not derivable
# from the team name (Athletics is "ATH", Arizona Diamondbacks is "AZ" not "ARI").
_TEAM_CODES: dict[str, str] = {
    "Los Angeles Angels": "LAA",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago White Sox": "CWS",
    "Cleveland Guardians": "CLE",
    "Detroit Tigers": "DET",
    "Kansas City Royals": "KC",
    "Minnesota Twins": "MIN",
    "New York Yankees": "NYY",
    "Athletics": "ATH",
    "Seattle Mariners": "SEA",
    "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Arizona Diamondbacks": "AZ",
    "Atlanta Braves": "ATL",
    "Chicago Cubs": "CHC",
    "Cincinnati Reds": "CIN",
    "Colorado Rockies": "COL",
    "Miami Marlins": "MIA",
    "Houston Astros": "HOU",
    "Los Angeles Dodgers": "LAD",
    "Milwaukee Brewers": "MIL",
    "Washington Nationals": "WSH",
    "New York Mets": "NYM",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "St. Louis Cardinals": "STL",
    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",
}


def _aggregate_batter_games(df: pd.DataFrame, team_names: dict, player_names: dict) -> list[dict]:
    """Per (game_pk, batter) totals from Statcast pitch-level rows.

    RBI comes from the run-scored delta on the play (post_bat_score minus
    bat_score) rather than a dedicated RBI column, which Statcast's raw
    pitch-level export does not have.
    """
    terminal = df[df["events"].notna()].copy()
    rows: list[dict] = []
    for (game_pk, batter_id), group in terminal.groupby(["game_pk", "batter"]):
        first = group.iloc[0]
        home_code = first["home_team"]
        away_code = first["away_team"]
        is_home = bool((group["inning_topbot"] == "Bot").iloc[0])
        team_code = home_code if is_home else away_code
        opponent_code = away_code if is_home else home_code

        hits = int(group["events"].isin(_HIT_EVENTS).sum())
        total_bases = int(sum(_TOTAL_BASES.get(e, 0) for e in group["events"]))
        at_bats = int(group["events"].isin(_AT_BAT_EVENTS).sum())
        walks = int(group["events"].isin(_WALK_EVENTS).sum())
        strikeouts = int(group["events"].isin(_STRIKEOUT_EVENTS).sum())
        rbis = int((group["post_bat_score"] - group["bat_score"]).clip(lower=0).sum())

        rows.append({
            "game_pk": int(game_pk),
            "game_date": first["game_date"],
            "player": player_names.get(batter_id),
            "team": team_names.get(team_code, team_code),
            "opponent": team_names.get(opponent_code, opponent_code),
            "is_home": is_home,
            "at_bats": at_bats,
            "hits": hits,
            "home_runs": int((group["events"] == "home_run").sum()),
            "rbis": rbis,
            "total_bases": total_bases,
            "strikeouts": strikeouts,
            "walks": walks,
        })
    return rows


def _derive_starters(df: pd.DataFrame, pitcher_names: dict) -> dict[tuple[int, str], str]:
    """(inning, inning_topbot) -> starter name, from the first pitcher seen in
    inning 1 of each half. Covers one game's worth of rows; fetch_mlb_batter_games
    below calls this once per game_pk group and re-keys the result by game_pk."""
    first_inning = df[df["inning"] == 1]
    starters: dict[tuple[int, str], str] = {}
    for half, group in first_inning.groupby("inning_topbot"):
        pitcher_id = group.iloc[0]["pitcher"]
        starters[(1, half)] = pitcher_names.get(pitcher_id)
    return starters


async def fetch_mlb_batter_games(start_date: str, end_date: str) -> list[MlbPlayerGame]:
    """MlbPlayerGame rows for every batter's game in the date range.

    Day/night context comes from schedule_and_record per team, joined by
    date; Statcast's own rows do not carry a day/night flag.
    """
    import pybaseball

    df = await to_thread(pybaseball.statcast, start_dt=start_date, end_dt=end_date)
    if df is None or df.empty:
        return []

    player_ids = pd.unique(df[["batter", "pitcher"]].values.ravel())
    player_names = await to_thread(_lookup_player_names, player_ids)
    team_names = {code: name for name, code in _TEAM_CODES.items()}

    starter_map: dict[tuple[int, str], str] = {}
    for game_pk, game_df in df.groupby("game_pk"):
        for (_, half), name in _derive_starters(game_df, player_names).items():
            starter_map[(game_pk, half)] = name

    aggregated = _aggregate_batter_games(df, team_names=team_names, player_names=player_names)
    game_numbers = _doubleheader_game_numbers(aggregated)

    seasons_by_team: dict[str, pd.DataFrame] = {}
    results: list[MlbPlayerGame] = []
    for row in aggregated:
        team = row["team"]
        if team not in seasons_by_team:
            season_year = pd.to_datetime(row["game_date"]).year
            team_code = _TEAM_CODES.get(team, team)
            seasons_by_team[team] = await to_thread(_schedule_for_team, team_code, season_year)
        game_number = game_numbers.get((team, row["game_pk"]), 1)
        context = _lookup_schedule_context(seasons_by_team[team], row["game_date"], game_number)
        results.append(
            MlbPlayerGame(
                season=pd.to_datetime(row["game_date"]).year,
                game_date=pd.to_datetime(row["game_date"], utc=True).to_pydatetime(),
                player=row["player"],
                team=row["team"],
                opponent=row["opponent"],
                opposing_pitcher=starter_map.get((row["game_pk"], "Bot" if row["is_home"] else "Top")),
                is_home=row["is_home"],
                day_night=context.get("day_night", "day"),
                at_bats=row["at_bats"],
                hits=row["hits"],
                home_runs=row["home_runs"],
                rbis=row["rbis"],
                total_bases=row["total_bases"],
                strikeouts=row["strikeouts"],
                walks=row["walks"],
            )
        )
    logger.info("mlb_stats: aggregated %d batter-games from %s to %s", len(results), start_date, end_date)
    return results


def _lookup_player_names(player_ids) -> dict:
    import pybaseball

    names: dict = {}
    for pid in player_ids:
        if pd.isna(pid):
            continue
        try:
            info = pybaseball.playerid_reverse_lookup([int(pid)], key_type="mlbam")
        except Exception as exc:  # pybaseball's lookup has no documented exception type
            logger.warning("mlb_stats: player lookup failed for id=%s: %s", pid, exc)
            continue
        if not info.empty:
            r = info.iloc[0]
            names[int(pid)] = f"{r['name_first'].title()} {r['name_last'].title()}"
    return names


def _schedule_for_team(team_code: str, season: int) -> pd.DataFrame:
    import pybaseball

    return pybaseball.schedule_and_record(season, team_code)


def _doubleheader_game_numbers(aggregated: list[dict]) -> dict[tuple[str, int], int]:
    """(team, game_pk) -> which game of that team's day this was (1st, 2nd, ...).

    Statcast carries no per-pitch timestamp usable for this (sv_id and
    tfs_zulu_deprecated were both null in a live pull), so game_pk ascending
    order within a team's calendar date stands in for chronological order --
    MLB assigns game_pks sequentially, so the earlier game of a doubleheader
    always has the lower game_pk.
    """
    pks_by_team_date: dict[tuple[str, str], set[int]] = {}
    for row in aggregated:
        key = (row["team"], str(pd.to_datetime(row["game_date"]).date()))
        pks_by_team_date.setdefault(key, set()).add(row["game_pk"])

    game_numbers: dict[tuple[str, int], int] = {}
    for (team, _date), pks in pks_by_team_date.items():
        for ordinal, pk in enumerate(sorted(pks), start=1):
            game_numbers[(team, pk)] = ordinal
    return game_numbers


def _parse_schedule_date(date_str) -> tuple[int, int, int] | None:
    """'Saturday, Apr 13 (1)' -> (4, 13, 1); month, day, doubleheader game number.

    Baseball-Reference marks the second game of a doubleheader with a "(2)"
    suffix on the Date field (verified live against schedule_and_record's raw
    scrape: 2024 NYY schedule has "Saturday, Apr 13 (1)" / "Saturday, Apr 13 (2)").
    The Date field carries no year, so callers must already know which season
    they're matching against.
    """
    match = re.match(r"^[A-Za-z]+,\s*([A-Za-z]+)\s+(\d+)(?:\s*\((\d)\))?$", str(date_str).strip())
    if not match:
        return None
    month_name, day_str, game_num_str = match.groups()
    try:
        month = datetime.strptime(month_name[:3], "%b").month
    except ValueError:
        return None
    return (month, int(day_str), int(game_num_str) if game_num_str else 1)


def _lookup_schedule_context(schedule: pd.DataFrame, game_date, game_number: int = 1) -> dict:
    if schedule is None or schedule.empty or "Date" not in schedule.columns:
        return {}
    target = pd.to_datetime(game_date).date()
    parsed = schedule["Date"].map(_parse_schedule_date)
    same_day = parsed.map(lambda p: p is not None and p[0] == target.month and p[1] == target.day)
    match = schedule[same_day]
    if match.empty:
        return {}
    if len(match) > 1:
        exact = match[parsed[same_day].map(lambda p: p[2] == game_number)]
        if not exact.empty:
            match = exact
    row = match.iloc[0]
    day_night = "night" if str(row.get("D/N", "")).strip().upper() == "N" else "day"
    return {"day_night": day_night}
