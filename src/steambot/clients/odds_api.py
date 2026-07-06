"""The Odds API client.

Free tier: 500 requests/month. Each call to /sports/{sport}/odds/
returns all in-season games with lines from every enabled bookmaker.
One call per day per sport is sufficient for a daily picks run.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from steambot.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome

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
    return [_parse_game(g) for g in raw]


def _parse_game(raw: dict) -> GameSnapshot:
    bookmakers = []
    for bm in raw.get("bookmakers", []):
        markets = []
        for mkt in bm.get("markets", []):
            outcomes = [
                Outcome(
                    name=o["name"],
                    price=int(o["price"]),
                    point=float(o["point"]) if "point" in o else None,
                )
                for o in mkt.get("outcomes", [])
            ]
            markets.append(MarketOdds(key=mkt["key"], outcomes=outcomes))
        bookmakers.append(
            BookmakerOdds(key=bm["key"], title=bm["title"], markets=markets)
        )
    return GameSnapshot(
        game_id=raw["id"],
        sport=raw["sport_key"],
        home_team=raw["home_team"],
        away_team=raw["away_team"],
        commence_time=datetime.fromisoformat(raw["commence_time"].rstrip("Z")),
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
