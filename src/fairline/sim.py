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
SIGMA_TOTAL = 10.0
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


def over_probability(expected: float, line: float) -> float:
    """P(total > line) under Normal(expected, SIGMA_TOTAL)."""
    return _phi((expected - line) / SIGMA_TOTAL)


def build_scoring_rates(games: list[dict]) -> tuple[dict, float]:
    """Per-team offensive and defensive deviations from the league scoring average.

    Only the latest season in the input counts: scoring environments and
    rosters shift enough year to year that mixing seasons blurs more than it
    smooths. Returns ({team: {"off": dev, "def": dev}}, league_avg_points).
    """
    latest = max((g["season"] for g in games), default=0)
    season_games = [g for g in games if g["season"] == latest]
    if not season_games:
        return {}, 0.0

    scored: dict[str, list[int]] = {}
    allowed: dict[str, list[int]] = {}
    all_points: list[int] = []
    for g in season_games:
        scored.setdefault(g["home_team"], []).append(g["home_score"])
        allowed.setdefault(g["home_team"], []).append(g["away_score"])
        scored.setdefault(g["away_team"], []).append(g["away_score"])
        allowed.setdefault(g["away_team"], []).append(g["home_score"])
        all_points.extend((g["home_score"], g["away_score"]))

    league_avg = sum(all_points) / len(all_points)
    rates = {
        team: {
            "off": sum(scored[team]) / len(scored[team]) - league_avg,
            "def": sum(allowed[team]) / len(allowed[team]) - league_avg,
        }
        for team in scored
    }
    return rates, league_avg


def expected_total(rates: dict, league_avg: float, home: str, away: str) -> float:
    """Expected combined score; unknown teams contribute league-average zeros."""
    zero = {"off": 0.0, "def": 0.0}
    h, a = rates.get(home, zero), rates.get(away, zero)
    return 2 * league_avg + h["off"] + a["off"] + h["def"] + a["def"]


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


def _market_points(game: GameSnapshot, market: str) -> dict[str, float]:
    """Sharp-book point per outcome name for one market, if present."""
    from fairline.agents.odds import best_sharp_book

    book = best_sharp_book(game)
    if book is None:
        return {}
    bm = next((b for b in game.bookmakers if b.key == book), None)
    mkt = next((m for m in bm.markets if m.key == market), None) if bm else None
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

    sim_input = [
        {
            "season": season_of(r.commence_time) if r.commence_time else 0,
            "home_team": r.home_team,
            "away_team": r.away_team,
            "home_score": r.home_score,
            "away_score": r.away_score,
        }
        for r in rows
    ]
    ratings = build_ratings(sim_input)
    rates, league_avg = build_scoring_rates(sim_input)

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
        points = _market_points(game, "spreads")
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
        total_line = _market_points(game, "totals").get("Over")
        if total_line is not None and (game.home_team, game.away_team, "totals") not in covered:
            exp_total = expected_total(rates, league_avg, game.home_team, game.away_team)
            model_lines.append(
                SimLine(
                    home_team=game.home_team,
                    away_team=game.away_team,
                    market="totals",
                    selection=f"Over {total_line:g}",
                    probability=over_probability(exp_total, total_line),
                )
            )

    logger.info(
        "sim_agent: %d model lines from %d results (%d caller-supplied kept)",
        len(model_lines),
        len(rows),
        len(caller_lines),
    )
    return {"sim_lines": caller_lines + model_lines}
