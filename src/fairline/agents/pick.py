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

from fairline.clients.odds_api import RETAIL_BOOKS
from fairline.state import (
    BookmakerOdds,
    FairLine,
    GameSnapshot,
    PickCandidate,
    SimLine,
    FairlineState,
    american_to_prob,
    blend_probability,
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
_DEFAULT_SIM_WEIGHT = 0.25  # sharp-dominant until the sim proves CLV on disagreements


def _sim_weight() -> float:
    try:
        return float(os.environ.get("FAIRLINE_SIM_WEIGHT", _DEFAULT_SIM_WEIGHT))
    except ValueError:
        return _DEFAULT_SIM_WEIGHT


def _sim_probability(
    sim_lines: list[SimLine], game: GameSnapshot, market: str, outcome_name: str
) -> float | None:
    """Caller-supplied sim probability for this game/market/side, or None."""
    for sl in sim_lines:
        if (
            sl.home_team == game.home_team
            and sl.away_team == game.away_team
            and sl.market == market
            and (sl.selection == outcome_name or sl.selection.startswith(outcome_name + " "))
        ):
            return sl.probability
    return None


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


def _format_trends(team_trends: dict, team: str) -> str:
    t = team_trends.get(team)
    if not t:
        return ""
    line = f"{team}: {t['su']} SU, {t['ats']} ATS, {t['ou']} O/U (last {t['n']})"
    if t.get("rest_days") is not None:
        line += f", {'B2B' if t.get('b2b') else str(t['rest_days']) + 'd rest'}"
        if t.get("games_last_7", 0) >= 3:
            line += f", {t['games_last_7']} games in 7d"
    return line


def _format_team_stats(team_stats: dict, team: str) -> str:
    """Up to 6 numeric fields, sorted by key so output is deterministic across
    the four sports' very different stat schemas (no per-sport field mapping)."""
    stats = team_stats.get(team)
    if not stats:
        return ""
    numeric = {k: v for k, v in stats.items() if isinstance(v, (int, float))}
    if not numeric:
        return ""
    top = sorted(numeric.items())[:6]
    return f"{team}: " + ", ".join(f"{k}={v}" for k, v in top)


def _format_steam(steam_signal: dict, game_id: str) -> str:
    events = steam_signal.get(game_id)
    if not events:
        return ""
    return "\n  Steam moves: " + "; ".join(events)


def _build_prompt(
    games: list[GameSnapshot],
    fair_lines: list[FairLine],
    team_trends: dict | None = None,
    game_weather: dict | None = None,
    team_injuries: dict | None = None,
    team_stats: dict | None = None,
    steam_signal: dict | None = None,
) -> str:
    lines_by_game: dict[str, list[FairLine]] = {}
    for fl in fair_lines:
        lines_by_game.setdefault(fl.game_id, []).append(fl)
    team_trends = team_trends or {}
    game_weather = game_weather or {}
    team_injuries = team_injuries or {}
    team_stats = team_stats or {}
    steam_signal = steam_signal or {}

    sections = []
    for game in games:
        fl_list = lines_by_game.get(game.game_id, [])
        if not fl_list:
            continue
        fl_text = "\n".join(
            f"  {fl.market}: {', '.join(f'{o}={p:.1%}' for o, p in zip(fl.outcomes, fl.fair_probs))} (source: {fl.source_book})"
            for fl in fl_list
        )
        trend_lines = [
            _format_trends(team_trends, t)
            for t in (game.home_team, game.away_team)
            if _format_trends(team_trends, t)
        ]
        trend_text = ("\n  Recent form: " + "; ".join(trend_lines)) if trend_lines else ""
        inj_lines = []
        for team in (game.home_team, game.away_team):
            inj = team_injuries.get(team)
            if inj:
                inj_lines.append(f"{team}: " + ", ".join(inj["notes"][:4]))
        inj_text = ("\n  Injuries: " + " | ".join(inj_lines)) if inj_lines else ""
        stats_lines = [
            _format_team_stats(team_stats, t)
            for t in (game.home_team, game.away_team)
            if _format_team_stats(team_stats, t)
        ]
        stats_text = ("\n  Season stats: " + "; ".join(stats_lines)) if stats_lines else ""
        steam_text = _format_steam(steam_signal, game.game_id)
        wx = game_weather.get(game.game_id)
        wx_text = ""
        if wx:
            wx_text = f"\n  Weather: wind {wx['wind_mph']:.0f} mph"
            if wx.get("temp_f") is not None:
                wx_text += f", {wx['temp_f']:.0f}F"
            if wx.get("precip_prob") is not None:
                wx_text += f", precip {wx['precip_prob']}%"
        sections.append(
            f"GAME: {game.away_team} @ {game.home_team} -- {game.commence_time.strftime('%a %b %d %I:%M %p UTC')}\n"
            f"  game_id: {game.game_id}\n"
            f"  Fair probabilities (no-vig):\n{fl_text}{trend_text}{stats_text}{wx_text}{inj_text}{steam_text}"
        )

    return (
        "You are a sharp sports betting analyst. Review the following games and their no-vig fair probabilities "
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


async def pick_agent(state: FairlineState) -> dict:
    """Generate pick candidates using Claude. Returns picks to the state for HITL review."""
    games = state.get("games", [])
    fair_lines = state.get("fair_lines", [])

    if not games or not fair_lines:
        logger.info("pick_agent: no games or fair lines -- skipping")
        return {"candidates": [], "error": None}

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    prompt = _build_prompt(
        games,
        fair_lines,
        state.get("team_trends"),
        state.get("game_weather"),
        state.get("team_injuries"),
        state.get("team_stats"),
        state.get("steam_signal"),
    )

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

        idx = next((i for i, o in enumerate(fl.outcomes) if selection.startswith(o) or o in selection), None)
        if idx is None:
            continue
        sharp_prob = fl.fair_probs[idx]
        sim_prob = _sim_probability(state.get("sim_lines", []), game, market, fl.outcomes[idx])
        blended_prob = blend_probability(sharp_prob, sim_prob, _sim_weight())

        retail = _best_retail_price(game, market, fl.outcomes[idx])
        if not retail:
            retail_book, retail_price = rp.get("best_book", ""), rp.get("best_price", -110)
        else:
            retail_book, retail_price = retail

        implied_prob = american_to_prob(retail_price)
        edge_pct = blended_prob - implied_prob
        if edge_pct < _MIN_EDGE_PCT:
            logger.debug("pick_agent: skipping %s edge=%.1f%% < threshold", selection, edge_pct)
            continue

        ev_pct = _compute_ev(blended_prob, retail_price)
        candidates.append(
            PickCandidate(
                pick_id=str(uuid.uuid4()),
                game_id=game_id,
                sport=game.sport,
                home_team=game.home_team,
                away_team=game.away_team,
                commence_time=game.commence_time,
                market=market,
                selection=selection,
                best_book=retail_book,
                best_price=retail_price,
                sharp_probability=sharp_prob,
                sim_probability=sim_prob,
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
