"""Tests for pure state logic: vig removal and probability math."""

import pytest

from steambot.state import american_to_prob, remove_vig, FairLine, PickCandidate
from datetime import datetime


def test_american_to_prob_negative_favorite():
    # -110 favorite: 110 / (110 + 100) = 52.38%
    prob = american_to_prob(-110)
    assert abs(prob - 0.5238) < 0.001


def test_american_to_prob_positive_underdog():
    # +110 dog: 100 / (110 + 100) = 47.62%
    prob = american_to_prob(110)
    assert abs(prob - 0.4762) < 0.001


def test_american_to_prob_even_money():
    prob = american_to_prob(-100)
    assert abs(prob - 0.5) < 0.001


def test_remove_vig_two_sided_market():
    # Typical -110/-110 spread: raw probs sum to ~1.0476 (4.76% overround)
    raw = [american_to_prob(-110), american_to_prob(-110)]
    fair = remove_vig(raw)
    assert abs(sum(fair) - 1.0) < 1e-9
    assert abs(fair[0] - 0.5) < 0.001
    assert abs(fair[1] - 0.5) < 0.001


def test_remove_vig_moneyline_favorite():
    # Pinnacle NFL: -170 favorite, +155 dog
    fav_raw = american_to_prob(-170)  # 62.96%
    dog_raw = american_to_prob(155)   # 39.22%
    fair = remove_vig([fav_raw, dog_raw])
    assert abs(sum(fair) - 1.0) < 1e-9
    assert fair[0] > fair[1]  # favorite still more likely


def test_remove_vig_preserves_ratio():
    # No-vig should preserve the relative probability ratio.
    raw = [0.60, 0.45]  # overround = 1.05
    fair = remove_vig(raw)
    ratio_raw = raw[0] / raw[1]
    ratio_fair = fair[0] / fair[1]
    assert abs(ratio_raw - ratio_fair) < 1e-9


def test_fair_line_model():
    fl = FairLine(
        game_id="abc123",
        market="spreads",
        outcomes=["Chiefs -3.5", "Raiders +3.5"],
        fair_probs=[0.53, 0.47],
        source_book="pinnacle",
    )
    assert abs(sum(fl.fair_probs) - 1.0) < 0.001
    assert fl.source_book == "pinnacle"


def test_pick_candidate_edge_positive():
    pick = PickCandidate(
        pick_id="pick-1",
        game_id="game-1",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=datetime(2026, 1, 15, 20, 0),
        market="spreads",
        selection="Kansas City Chiefs -3.5",
        best_book="draftkings",
        best_price=-108,
        sharp_probability=0.545,
        blended_probability=0.545,
        implied_probability=0.519,
        edge_pct=0.026,
        ev_pct=0.031,
        confidence="medium",
        rationale="Sharp line moved from -3 to -3.5 against public action.",
    )
    assert pick.edge_pct > 0
    assert pick.confidence == "medium"


def test_pick_candidate_risk_flags_default_empty():
    pick = PickCandidate(
        pick_id="p2",
        game_id="g2",
        home_team="A",
        away_team="B",
        commence_time=datetime(2026, 1, 15),
        market="h2h",
        selection="A",
        best_book="fanduel",
        best_price=-115,
        sharp_probability=0.54,
        blended_probability=0.54,
        implied_probability=0.535,
        edge_pct=0.005,
        ev_pct=0.003,
        confidence="low",
        rationale="Small edge.",
    )
    assert pick.risk_flags == []
