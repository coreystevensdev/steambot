"""OddsAgent node.

Fetches live NFL odds, computes no-vig fair probabilities from the sharp-line
reference (Pinnacle first; best available book if Pinnacle is absent), and
stores both the raw game snapshots and derived fair lines on the state.
"""

from __future__ import annotations

import logging
import uuid

import httpx

from steambot.clients.odds_api import SHARP_BOOKS, fetch_nfl_odds
from steambot.state import (
    FairLine,
    GameSnapshot,
    SteamBotState,
    american_to_prob,
    remove_vig,
)

logger = logging.getLogger(__name__)

# Prefer sharp books in order; fall back to any bookmaker present.
_SHARP_PRIORITY = ["pinnacle", "betonlineag", "mybookieag"]


def best_sharp_book(game: GameSnapshot) -> str | None:
    book_keys = {bm.key for bm in game.bookmakers}
    for key in _SHARP_PRIORITY:
        if key in book_keys:
            return key
    return None


def _derive_fair_line(game: GameSnapshot, market_key: str, source_book: str) -> FairLine | None:
    bm = next((b for b in game.bookmakers if b.key == source_book), None)
    if not bm:
        return None
    mkt = next((m for m in bm.markets if m.key == market_key), None)
    if not mkt or len(mkt.outcomes) < 2:
        return None
    raw_probs = [american_to_prob(o.price) for o in mkt.outcomes]
    fair = remove_vig(raw_probs)
    return FairLine(
        game_id=game.game_id,
        market=market_key,
        outcomes=[o.name for o in mkt.outcomes],
        fair_probs=fair,
        source_book=source_book,
    )


async def odds_agent(state: SteamBotState, client: httpx.AsyncClient) -> dict:
    """Fetch current NFL odds and compute fair lines from the sharp reference."""
    logger.info("odds_agent: fetching NFL odds for date=%s", state.get("target_date"))
    try:
        games = await fetch_nfl_odds(client)
    except Exception as exc:
        logger.error("odds_agent: fetch failed: %s", exc)
        return {"error": f"Odds fetch failed: {exc}", "games": [], "fair_lines": []}

    fair_lines: list[FairLine] = []
    for game in games:
        sharp_book = best_sharp_book(game)
        if not sharp_book:
            logger.warning("game=%s has no sharp-line reference -- skipping", game.game_id)
            continue
        for market in ("h2h", "spreads", "totals"):
            fl = _derive_fair_line(game, market, sharp_book)
            if fl:
                fair_lines.append(fl)

    logger.info(
        "odds_agent: found %d games, derived %d fair lines",
        len(games),
        len(fair_lines),
    )
    return {"games": games, "fair_lines": fair_lines, "error": None}
