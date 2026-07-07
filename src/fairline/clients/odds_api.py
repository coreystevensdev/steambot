"""The Odds API client.

Free tier: 500 requests/month. Each call to /sports/{sport}/odds/
returns all in-season games with lines from every enabled bookmaker.
One call per day per sport is sufficient for a daily picks run.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from fairline.state import BookmakerOdds, GameScore, GameSnapshot, MarketOdds, Outcome

logger = logging.getLogger(__name__)

_BASE = "https://api.the-odds-api.com/v4"

SUPPORTED_SPORTS = {
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
}

# Pinnacle is the primary sharp-line reference. FanDuel/DraftKings as retail.
SHARP_BOOKS = {"pinnacle"}
RETAIL_BOOKS = {"fanduel", "draftkings", "betmgm", "caesars", "pointsbet"}


def _api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("ODDS_API_KEY is not set")
    return key


async def fetch_odds(
    client: httpx.AsyncClient,
    sport: str,
    markets: str = "h2h,spreads,totals",
    regions: str = "us",
    odds_format: str = "american",
) -> list[GameSnapshot]:
    """Fetch current odds for one sport from all enabled bookmakers.

    Returns an empty list when no games are scheduled (off-season).
    Raises httpx.HTTPStatusError on API errors (e.g., quota exceeded).
    """
    if sport not in SUPPORTED_SPORTS:
        raise ValueError(f"unsupported sport {sport!r}; supported: {sorted(SUPPORTED_SPORTS)}")
    params = {
        "apiKey": _api_key(),
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    resp = await client.get(
        f"{_BASE}/sports/{sport}/odds/",
        params=params,
        timeout=httpx.Timeout(30.0),
    )
    resp.raise_for_status()
    raw: list[dict] = resp.json()
    games = []
    for g in raw:
        parsed = _parse_game(g)
        if parsed is not None:
            games.append(parsed)
    return games


def _parse_game(raw: dict) -> GameSnapshot | None:
    """Map one Odds API game element to a GameSnapshot.

    Returns None when a required field is missing or malformed so a single bad
    record does not sink the whole batch. Bookmakers and outcomes that fail to
    parse are skipped individually.
    """
    game_id = raw.get("id")
    sport_key = raw.get("sport_key")
    home_team = raw.get("home_team")
    away_team = raw.get("away_team")
    commence_time = raw.get("commence_time")
    if not (game_id and sport_key and home_team and away_team and commence_time):
        logger.warning("odds_api: skipping game with missing required fields: id=%r", raw.get("id"))
        return None

    try:
        parsed_time = datetime.fromisoformat(commence_time.rstrip("Z"))
    except (ValueError, AttributeError):
        logger.warning("odds_api: skipping game %r with bad commence_time=%r", game_id, commence_time)
        return None

    bookmakers = []
    for bm in raw.get("bookmakers", []):
        bm_key = bm.get("key")
        bm_title = bm.get("title")
        if not (bm_key and bm_title):
            logger.warning("odds_api: skipping bookmaker with missing key/title in game %r", game_id)
            continue
        markets = []
        for mkt in bm.get("markets", []):
            mkt_key = mkt.get("key")
            if not mkt_key:
                logger.warning("odds_api: skipping market with no key in game %r book %r", game_id, bm_key)
                continue
            outcomes = []
            for o in mkt.get("outcomes", []):
                name = o.get("name")
                price = o.get("price")
                if name is None or price is None:
                    logger.warning(
                        "odds_api: skipping outcome missing name/price in game %r book %r market %r",
                        game_id, bm_key, mkt_key,
                    )
                    continue
                try:
                    outcomes.append(
                        Outcome(
                            name=name,
                            price=int(price),
                            point=float(o["point"]) if o.get("point") is not None else None,
                        )
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "odds_api: skipping outcome with bad price/point in game %r book %r", game_id, bm_key
                    )
            markets.append(MarketOdds(key=mkt_key, outcomes=outcomes))
        bookmakers.append(BookmakerOdds(key=bm_key, title=bm_title, markets=markets))

    return GameSnapshot(
        game_id=game_id,
        sport=sport_key,
        home_team=home_team,
        away_team=away_team,
        commence_time=parsed_time,
        bookmakers=bookmakers,
    )


async def fetch_scores(
    client: httpx.AsyncClient, sport: str, days_from: int = 3
) -> list[GameScore]:
    """Fetch scores for one sport's recent and upcoming games.

    daysFrom reaches at most 3 days back (API maximum); games older than that
    are gone from the feed and can never be graded from this endpoint.
    """
    if sport not in SUPPORTED_SPORTS:
        raise ValueError(f"unsupported sport {sport!r}; supported: {sorted(SUPPORTED_SPORTS)}")
    params = {"apiKey": _api_key(), "daysFrom": days_from, "dateFormat": "iso"}
    resp = await client.get(
        f"{_BASE}/sports/{sport}/scores/",
        params=params,
        timeout=httpx.Timeout(30.0),
    )
    resp.raise_for_status()
    scores = []
    for raw in resp.json():
        parsed = _parse_score(raw)
        if parsed is not None:
            scores.append(parsed)
    return scores


def _parse_score(raw: dict) -> GameScore | None:
    game_id = raw.get("id")
    home_team = raw.get("home_team")
    away_team = raw.get("away_team")
    if not (game_id and home_team and away_team):
        logger.warning("odds_api: skipping score with missing fields: id=%r", raw.get("id"))
        return None

    commence_time = None
    raw_commence = raw.get("commence_time")
    if raw_commence:
        try:
            commence_time = datetime.fromisoformat(raw_commence.rstrip("Z"))
        except (ValueError, AttributeError):
            commence_time = None

    home_score = away_score = None
    for entry in raw.get("scores") or []:
        try:
            value = int(entry.get("score"))
        except (TypeError, ValueError):
            continue
        if entry.get("name") == home_team:
            home_score = value
        elif entry.get("name") == away_team:
            away_score = value

    return GameScore(
        game_id=game_id,
        completed=bool(raw.get("completed")),
        commence_time=commence_time,
        home_team=home_team,
        away_team=away_team,
        home_score=home_score,
        away_score=away_score,
    )


async def fetch_sports(client: httpx.AsyncClient) -> list[dict]:
    """List all available sports (useful for health check and discovery)."""
    resp = await client.get(
        f"{_BASE}/sports/",
        params={"apiKey": _api_key()},
        timeout=httpx.Timeout(15.0),
    )
    resp.raise_for_status()
    return resp.json()
