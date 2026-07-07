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

from steambot.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome

logger = logging.getLogger(__name__)

_BASE = "https://api.the-odds-api.com/v4"

# Pinnacle is the primary sharp-line reference. FanDuel/DraftKings as retail.
SHARP_BOOKS = {"pinnacle"}
RETAIL_BOOKS = {"fanduel", "draftkings", "betmgm", "caesars", "pointsbet"}


def _api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("ODDS_API_KEY is not set")
    return key


async def fetch_nfl_odds(
    client: httpx.AsyncClient,
    markets: str = "h2h,spreads,totals",
    regions: str = "us",
    odds_format: str = "american",
) -> list[GameSnapshot]:
    """Fetch current NFL odds from all enabled bookmakers.

    Returns an empty list when no games are scheduled (off-season).
    Raises httpx.HTTPStatusError on API errors (e.g., quota exceeded).
    """
    params = {
        "apiKey": _api_key(),
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }
    resp = await client.get(
        f"{_BASE}/sports/americanfootball_nfl/odds/",
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


async def fetch_sports(client: httpx.AsyncClient) -> list[dict]:
    """List all available sports (useful for health check and discovery)."""
    resp = await client.get(
        f"{_BASE}/sports/",
        params={"apiKey": _api_key()},
        timeout=httpx.Timeout(15.0),
    )
    resp.raise_for_status()
    return resp.json()
