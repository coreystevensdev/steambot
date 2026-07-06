"""PickAgent node.

Uses Claude with a forced `submit_picks` tool call to generate structured
pick recommendations. Blends the sharp fair probability with any simulation
adjustment and computes edge vs. the best available retail price.

Claude sees: game metadata, fair lines, team context (if ResearchAgent ran).
Claude never sees: raw API responses, user PII, or financial balances.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from datetime import datetime

import anthropic

from steambot.clients.odds_api import RETAIL_BOOKS
from steambot.state import (
    BookmakerOdds,
    FairLine,
    GameSnapshot,
    PickCandidate,
    SteamBotState,
    american_to_prob,
)

logger = logging.getLogger(__name__)

_SUBMIT_PICKS_TOOL = {
    "name": "submit_picks",
    "description": "Submit structured pick recommendations for human review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "game_id": {"type": "string"},
                        "market": {"type": "string"},
                        "selection": {"type": "string"},
                        "best_book": {"type": "string"},
                        "best_price": {"type": "integer"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "rationale": {"type": "string"},
                        "risk_flags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["game_id", "market", "selection", "best_book", "best_price", "confidence", "rationale"],
                },
            },
            "analyst_notes": {"type": "string"},
        },
        "required": ["picks"],
    },
}

_MIN_EDGE_PCT = 0.02  # only surface picks with at least 2% edge


def _best_retail_price(game: GameSnapshot, market_key: str, selection: str) -> tuple[str, int] | None:
    """Find the best American price for a selection across retail books."""
    best_book, best_price = None, None
    for bm in game.bookmakers:
        if bm.key not in RETAIL_BOOKS:
            continue
        mkt = next((m for m in bm.markets if m.key == market_key), None)
        if not mkt:
            continue
        for outcome in mkt.outcomes:
            if outcome.name == selection:
                # Higher American odds = better price
                if best_price is None or outcome.price > best_price:
                    best_book = bm.key
                    best_price = outcome.price
    if best_book and best_price is not None:
        return best_book, best_price
    return None


def _compute_ev(blended_prob: float, american_price: int) -> float:
    """EV = blended_prob * win_amount - (1 - blended_prob) * 1.0 (per unit wagered)."""
    if american_price > 0:
        win_amount = american_price / 100
    else:
        win_amount = 100 / abs(american_price)
    return blended_prob * win_amount - (1 - blended_prob) * 1.0


def _build_prompt(games: list[GameSnapshot], fair_lines: list[FairLine]) -> str:
    lines_by_game: dict[str, list[FairLine]] = {}
    for fl in fair_lines:
        lines_by_game.setdefault(fl.game_id, []).append(fl)

    sections = []
    for game in games:
        fl_list = lines_by_game.get(game.game_id, [])
        if not fl_list:
            continue
        fl_text = "\n".join(
            f"  {fl.market}: {', '.join(f'{o}={p:.1%}' for o, p in zip(fl.outcomes, fl.fair_probs))} (source: {fl.source_book})"
            for fl in fl_list
        )
        sections.append(
            f"GAME: {game.away_team} @ {game.home_team} -- {game.commence_time.strftime('%a %b %d %I:%M %p UTC')}\n"
            f"  game_id: {game.game_id}\n"
            f"  Fair probabilities (no-vig):\n{fl_text}"
        )

    return (
        "You are a sharp sports betting analyst. Review the following NFL games and their no-vig fair probabilities "
        "derived from the sharp-line reference. Identify the highest-edge betting opportunities.\n\n"
        "For each recommended pick:\n"
        "- Market types: h2h (moneyline), spreads (against the spread), totals (over/under)\n"
        "- Confidence HIGH requires clear market inefficiency (edge >5%). MEDIUM for 3-5%. LOW for 2-3%.\n"
        "- Risk flags: injury uncertainty, weather, divisional game variance, scheduling spots.\n"
        "- Rationale must explain WHY this line has edge, not just describe the matchup.\n"
        "- Only recommend picks where there is genuine reason to believe the market is mispriced.\n\n"
        "Games:\n" + "\n\n".join(sections) + "\n\n"
        "Use the submit_picks tool to return your analysis."
    )


async def pick_agent(state: SteamBotState) -> dict:
    """Generate pick candidates using Claude. Returns picks to the state for HITL review."""
    games = state.get("games", [])
    fair_lines = state.get("fair_lines", [])

    if not games or not fair_lines:
        logger.info("pick_agent: no games or fair lines -- skipping")
        return {"candidates": [], "error": None}

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    prompt = _build_prompt(games, fair_lines)

    resp = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=4096,
        tools=[_SUBMIT_PICKS_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": prompt}],
    )

    tool_block = next((b for b in resp.content if b.type == "tool_use" and b.name == "submit_picks"), None)
    if not tool_block:
        logger.warning("pick_agent: Claude did not call submit_picks")
        return {"candidates": [], "error": "Claude did not call submit_picks"}

    raw_picks = tool_block.input.get("picks", [])
    game_map = {g.game_id: g for g in games}
    fair_map: dict[tuple[str, str], FairLine] = {(fl.game_id, fl.market): fl for fl in fair_lines}

    candidates: list[PickCandidate] = []
    for rp in raw_picks:
        game_id = rp.get("game_id", "")
        market = rp.get("market", "")
        selection = rp.get("selection", "")

        game = game_map.get(game_id)
        fl = fair_map.get((game_id, market))
        if not game or not fl:
            continue

        # Identify which fair_prob maps to this selection
        idx = next((i for i, o in enumerate(fl.outcomes) if selection.startswith(o) or o in selection), None)
        if idx is None:
            continue
        sharp_prob = fl.fair_probs[idx]
        blended_prob = sharp_prob  # no sim layer yet; extend here

        retail = _best_retail_price(game, market, fl.outcomes[idx])
        if not retail:
            retail_book, retail_price = rp.get("best_book", ""), rp.get("best_price", -110)
        else:
            retail_book, retail_price = retail

        implied_prob = american_to_prob(retail_price)
        edge_pct = blended_prob - implied_prob
        if edge_pct < _MIN_EDGE_PCT:
            logger.debug("pick_agent: skipping %s edge=%.1%% < threshold", selection, edge_pct)
            continue

        ev_pct = _compute_ev(blended_prob, retail_price)
        candidates.append(
            PickCandidate(
                pick_id=str(uuid.uuid4()),
                game_id=game_id,
                home_team=game.home_team,
                away_team=game.away_team,
                commence_time=game.commence_time,
                market=market,
                selection=selection,
                best_book=retail_book,
                best_price=retail_price,
                sharp_probability=sharp_prob,
                blended_probability=blended_prob,
                implied_probability=implied_prob,
                edge_pct=edge_pct,
                ev_pct=ev_pct,
                confidence=rp.get("confidence", "low"),
                rationale=rp.get("rationale", ""),
                risk_flags=rp.get("risk_flags", []),
            )
        )

    candidates.sort(key=lambda p: p.edge_pct, reverse=True)
    logger.info("pick_agent: generated %d candidates", len(candidates))
    return {"candidates": candidates, "error": None}
