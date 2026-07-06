"""LangGraph state schema and Pydantic models for SteamBot.

All agent nodes operate on SteamBotState. The HITL interrupt checkpoint
lives between generate_picks and finalize_picks; the graph saves its state
to PostgresSaver so the approval session survives server restarts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ---------------------------------------------------------------------------
# Odds and market models
# ---------------------------------------------------------------------------


class Outcome(BaseModel):
    name: str
    price: int  # American odds
    point: float | None = None  # spread or total line


class MarketOdds(BaseModel):
    key: str  # "h2h" | "spreads" | "totals"
    outcomes: list[Outcome]


class BookmakerOdds(BaseModel):
    key: str  # e.g. "pinnacle", "draftkings"
    title: str
    markets: list[MarketOdds]


class GameSnapshot(BaseModel):
    game_id: str
    sport: str
    home_team: str
    away_team: str
    commence_time: datetime
    bookmakers: list[BookmakerOdds]


# ---------------------------------------------------------------------------
# Fair probability and edge models
# ---------------------------------------------------------------------------


def american_to_prob(odds: int) -> float:
    """Convert American odds to raw implied probability (before vig removal)."""
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def remove_vig(probs: list[float]) -> list[float]:
    """Normalize a list of raw implied probabilities to sum to 1.0."""
    total = sum(probs)
    if total <= 0:
        return probs
    return [p / total for p in probs]


class FairLine(BaseModel):
    """No-vig fair probability derived from a single bookmaker's market."""
    game_id: str
    market: str
    outcomes: list[str]
    fair_probs: list[float]  # indexed to match outcomes; sum to 1.0
    source_book: str


# ---------------------------------------------------------------------------
# Pick and recommendation models
# ---------------------------------------------------------------------------


class PickCandidate(BaseModel):
    pick_id: str
    game_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    market: str  # "spreads" | "totals" | "h2h"
    selection: str  # e.g., "Kansas City Chiefs -3.5"
    best_book: str
    best_price: int  # American odds
    sharp_probability: float  # no-vig Pinnacle (or best sharp) fair prob
    sim_probability: float | None = None  # model simulation if available
    blended_probability: float  # final blended fair prob
    implied_probability: float  # retail book's raw implied prob (pre-vig)
    edge_pct: float  # blended_probability - implied_probability
    ev_pct: float  # expected value as a % of wager
    confidence: Literal["high", "medium", "low"]
    rationale: str
    risk_flags: list[str] = Field(default_factory=list)
    approved: bool | None = None


class ApprovedPick(BaseModel):
    pick: PickCandidate
    approved_at: datetime
    user_id: str


class BetSlip(BaseModel):
    pick_id: str
    selection: str
    book: str
    price: int
    stake_units: float
    prepared_at: datetime


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class SteamBotState(TypedDict):
    # Input
    sport: str
    target_date: str  # ISO date, e.g., "2026-01-15"
    user_id: str

    # OddsAgent output
    games: list[GameSnapshot]
    fair_lines: list[FairLine]

    # PickAgent output
    candidates: list[PickCandidate]

    # HITL approval (populated after interrupt + resume)
    approved_picks: list[ApprovedPick]

    # Bet slip preparation
    bet_slips: list[BetSlip]

    # Run metadata
    run_id: str
    error: str | None
