"""Ratings-based simulation model for NFL (design: docs/sim-design.md).

Elo-style points-scale ratings fit from game results, a Normal margin model,
and a graph node that turns them into sim_lines for the pick blend. All
numbers come from math over data; the LLM is never asked for a probability.

The nflverse games file also carries closing spreads and totals, so one
download backfills both the ratings input and the trends table.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from datetime import datetime

from sqlalchemy import select

from fairline.db.models import GameResult
from fairline.state import FairlineState, GameSnapshot, SimLine

logger = logging.getLogger(__name__)

NFLVERSE_GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

HFA_POINTS = 2.0
SIGMA_MARGIN = 13.5
ELO_K = 0.06
SEASON_CARRYOVER = 2 / 3  # regress a third of each rating away between seasons

# nflverse team codes to The Odds API's full names. Codes for 2021+ seasons;
# relocated-franchise legacy codes (OAK, SD, STL) are deliberately absent.
NFL_TEAMS = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def win_probability(expected_margin: float) -> float:
    """P(margin > 0) under Normal(expected_margin, SIGMA_MARGIN)."""
    return _phi(expected_margin / SIGMA_MARGIN)


def cover_probability(expected_margin: float, team_point: float) -> float:
    """P(margin + team_point > 0): the team covers its spread."""
    return _phi((expected_margin + team_point) / SIGMA_MARGIN)


def season_of(when: datetime) -> int:
    """NFL season a date belongs to; January playoff games are prior-season."""
    return when.year if when.month >= 8 else when.year - 1


def build_ratings(games: list[dict], k: float = ELO_K, hfa: float = HFA_POINTS) -> dict[str, float]:
    """Points-scale ratings from chronological results.

    Each game moves both teams toward the observed margin by k times the
    prediction error. Between seasons every rating regresses toward zero:
    rosters turn over, and last year's 12-point team is not this year's.
    """
    ratings: dict[str, float] = {}
    current_season: int | None = None
    for g in games:
        if current_season is not None and g["season"] != current_season:
            ratings = {t: r * SEASON_CARRYOVER for t, r in ratings.items()}
        current_season = g["season"]

        home, away = g["home_team"], g["away_team"]
        expected = ratings.get(home, 0.0) - ratings.get(away, 0.0) + hfa
        error = (g["home_score"] - g["away_score"]) - expected
        ratings[home] = ratings.get(home, 0.0) + k * error
        ratings[away] = ratings.get(away, 0.0) - k * error
    return ratings


def parse_nflverse_games(csv_text: str) -> tuple[list[dict], list[GameResult]]:
    """Parse the nflverse games file into ratings input and trends rows.

    Rows without scores (future games) are skipped. spread_line is the points
    the home team is favored by, so the home handicap flips its sign.
    """
    sim_games: list[dict] = []
    results: list[GameResult] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        home = NFL_TEAMS.get(row.get("home_team", ""))
        away = NFL_TEAMS.get(row.get("away_team", ""))
        if not home or not away:
            continue
        try:
            home_score = int(row["home_score"])
            away_score = int(row["away_score"])
            season = int(row["season"])
        except (KeyError, TypeError, ValueError):
            continue

        sim_games.append(
            {
                "season": season,
                "home_team": home,
                "away_team": away,
                "home_score": home_score,
                "away_score": away_score,
            }
        )

        def _line(key: str) -> float | None:
            try:
                return float(row[key])
            except (KeyError, TypeError, ValueError):
                return None

        commence = None
        try:
            commence = datetime.fromisoformat(row["gameday"])
        except (KeyError, TypeError, ValueError):
            pass
        spread_line = _line("spread_line")
        results.append(
            GameResult(
                game_id=row.get("game_id") or f"{season}_{row['home_team']}_{row['away_team']}",
                sport="americanfootball_nfl",
                home_team=home,
                away_team=away,
                commence_time=commence,
                home_score=home_score,
                away_score=away_score,
                closing_spread_home=-spread_line if spread_line is not None else None,
                closing_total=_line("total_line"),
            )
        )
    return sim_games, results


def _spread_points(game: GameSnapshot) -> dict[str, float]:
    """Sharp-book spread point per team name, if a spreads market exists."""
    from fairline.agents.odds import best_sharp_book

    book = best_sharp_book(game)
    if book is None:
        return {}
    bm = next((b for b in game.bookmakers if b.key == book), None)
    mkt = next((m for m in bm.markets if m.key == "spreads"), None) if bm else None
    if mkt is None:
        return {}
    return {o.name: o.point for o in mkt.outcomes if o.point is not None}


async def sim_agent(state: FairlineState, session_factory=None) -> dict:
    """Compute model probabilities for the slate from stored results.

    Caller-supplied sim lines keep the last word: the model only fills
    game/market combinations the request left empty. NFL only for now; other
    sports pass through untouched (their margin distributions need different
    families, per the design doc).
    """
    caller_lines: list[SimLine] = list(state.get("sim_lines", []))
    games = state.get("games", [])
    sport = state.get("sport", "americanfootball_nfl")
    if not games or session_factory is None or sport != "americanfootball_nfl":
        return {"sim_lines": caller_lines}

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(GameResult)
                    .where(GameResult.sport == sport)
                    .order_by(GameResult.commence_time)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return {"sim_lines": caller_lines}

    ratings = build_ratings(
        [
            {
                "season": season_of(r.commence_time) if r.commence_time else 0,
                "home_team": r.home_team,
                "away_team": r.away_team,
                "home_score": r.home_score,
                "away_score": r.away_score,
            }
            for r in rows
        ]
    )

    covered = {(sl.home_team, sl.away_team, sl.market) for sl in caller_lines}
    model_lines: list[SimLine] = []
    for game in games:
        expected = (
            ratings.get(game.home_team, 0.0) - ratings.get(game.away_team, 0.0) + HFA_POINTS
        )
        if (game.home_team, game.away_team, "h2h") not in covered:
            model_lines.append(
                SimLine(
                    home_team=game.home_team,
                    away_team=game.away_team,
                    market="h2h",
                    selection=game.home_team,
                    probability=win_probability(expected),
                )
            )
        points = _spread_points(game)
        home_point = points.get(game.home_team)
        if home_point is not None and (game.home_team, game.away_team, "spreads") not in covered:
            model_lines.append(
                SimLine(
                    home_team=game.home_team,
                    away_team=game.away_team,
                    market="spreads",
                    selection=f"{game.home_team} {home_point:+g}",
                    probability=cover_probability(expected, home_point),
                )
            )

    logger.info(
        "sim_agent: %d model lines from %d results (%d caller-supplied kept)",
        len(model_lines),
        len(rows),
        len(caller_lines),
    )
    return {"sim_lines": caller_lines + model_lines}
