"""Pick measurement: closing-line settlement and result grading.

The free Odds API tier has no historical endpoint, so the closing line is the
last sharp-book snapshot taken near kickoff. Run `python -m steambot settle`
shortly before games start; picks whose window is missed keep clv NULL.
`python -m steambot grade` fills result and profit_units from final scores.

CLV = no-vig closing probability - implied probability of the taken price.
Positive means the bet beat the close, regardless of what the model thought.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import select

from steambot.agents.odds import best_sharp_book
from steambot.db.models import Pick
from steambot.state import GameScore, GameSnapshot, american_to_prob, remove_vig

logger = logging.getLogger(__name__)


class ClosingLine(NamedTuple):
    price: int
    probability: float
    book: str
    point: float | None


def closing_line_for_selection(game: GameSnapshot, market: str, selection: str) -> ClosingLine | None:
    """No-vig closing probability for one side of a market, or None if absent."""
    book = best_sharp_book(game)
    if book is None:
        return None
    bm = next((b for b in game.bookmakers if b.key == book), None)
    mkt = next((m for m in bm.markets if m.key == market), None) if bm else None
    if mkt is None or len(mkt.outcomes) < 2:
        return None

    # Selections are stored as "name point" ("Kansas City Chiefs -3.5",
    # "Over 47.5") or bare name for h2h; match on the name prefix.
    idx = next(
        (
            i
            for i, o in enumerate(mkt.outcomes)
            if selection == o.name or selection.startswith(o.name + " ")
        ),
        None,
    )
    if idx is None:
        return None

    fair = remove_vig([american_to_prob(o.price) for o in mkt.outcomes])
    matched = mkt.outcomes[idx]
    return ClosingLine(price=matched.price, probability=fair[idx], book=book, point=matched.point)


async def settle_closing_lines(
    games: list[GameSnapshot],
    session_factory,
    now: datetime | None = None,
    window_minutes: int = 30,
) -> dict:
    """Fill closing_price, closing_probability, and clv on unsettled picks.

    Only picks whose game starts within window_minutes (or already started)
    are attempted; the rest count as pending. Games missing from the feed or
    selections that no longer match count as missed and stay NULL.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=window_minutes)
    games_by_id = {g.game_id: g for g in games}

    settled = missed = pending = 0
    async with session_factory() as session:
        rows = (
            (await session.execute(select(Pick).where(Pick.closing_price.is_(None))))
            .scalars()
            .all()
        )
        for pick in rows:
            commence = pick.commence_time
            # SQLite returns naive datetimes; rows are always written as UTC
            if commence.tzinfo is None:
                commence = commence.replace(tzinfo=timezone.utc)
            if commence > cutoff:
                pending += 1
                continue

            game = games_by_id.get(pick.game_id)
            line = closing_line_for_selection(game, pick.market, pick.selection) if game else None
            if line is None:
                missed += 1
                logger.warning(
                    "settle: no closing line for pick_id=%s game_id=%s selection=%r",
                    pick.id,
                    pick.game_id,
                    pick.selection,
                )
                continue

            pick.closing_price = line.price
            pick.closing_point = line.point
            pick.closing_probability = line.probability
            pick.clv = line.probability - american_to_prob(pick.price)
            settled += 1
            logger.info(
                "settle: pick_id=%s closed at %d (%.4f), clv=%+.4f via %s",
                pick.id,
                line.price,
                line.probability,
                pick.clv,
                line.book,
            )
        await session.commit()

    return {"settled": settled, "missed": missed, "pending": pending}


def _profit_units(price: int, result: str) -> float:
    if result == "win":
        return price / 100 if price > 0 else 100 / abs(price)
    return -1.0 if result == "loss" else 0.0


def grade_pick(
    market: str, selection: str, price: int, score: GameScore
) -> tuple[str, float] | None:
    """Grade one pick against a final score: (result, profit_units) or None.

    Ties grade as push for h2h since the stored markets are two-way; a draw
    refunds the stake at US books rather than losing.
    """
    if not score.completed or score.home_score is None or score.away_score is None:
        return None

    if market == "totals":
        side, _, point_str = selection.partition(" ")
        try:
            point = float(point_str)
        except ValueError:
            return None
        if side not in ("Over", "Under"):
            return None
        total = score.home_score + score.away_score
        diff = total - point if side == "Over" else point - total
    elif market in ("h2h", "spreads"):
        if selection == score.home_team or selection.startswith(score.home_team + " "):
            team, team_score, opp_score = score.home_team, score.home_score, score.away_score
        elif selection == score.away_team or selection.startswith(score.away_team + " "):
            team, team_score, opp_score = score.away_team, score.away_score, score.home_score
        else:
            return None
        margin = team_score - opp_score
        if market == "h2h":
            diff = float(margin)
        else:
            try:
                point = float(selection[len(team):].strip())
            except ValueError:
                return None
            diff = margin + point
    else:
        return None

    result = "win" if diff > 0 else "loss" if diff < 0 else "push"
    return result, _profit_units(price, result)


async def grade_results(scores: list[GameScore], session_factory) -> dict:
    """Fill result and profit_units on ungraded picks from final scores.

    Incomplete games count as pending; games absent from the feed (the scores
    endpoint reaches back at most 3 days) or unmatched selections count as
    missed and stay NULL.
    """
    scores_by_id = {s.game_id: s for s in scores}

    graded = pending = missed = 0
    async with session_factory() as session:
        rows = (
            (await session.execute(select(Pick).where(Pick.result.is_(None))))
            .scalars()
            .all()
        )
        for pick in rows:
            score = scores_by_id.get(pick.game_id)
            if score is None:
                missed += 1
                logger.warning(
                    "grade: game_id=%s for pick_id=%s not in scores feed", pick.game_id, pick.id
                )
                continue
            if not score.completed:
                pending += 1
                continue
            outcome = grade_pick(pick.market, pick.selection, pick.price, score)
            if outcome is None:
                missed += 1
                logger.warning(
                    "grade: could not grade pick_id=%s selection=%r", pick.id, pick.selection
                )
                continue
            pick.result, pick.profit_units = outcome
            graded += 1
            logger.info(
                "grade: pick_id=%s %s %+.4f units", pick.id, pick.result, pick.profit_units
            )
        await session.commit()

    return {"graded": graded, "pending": pending, "missed": missed}


async def sim_clv_report(session_factory, disagree_threshold: float = 0.02) -> dict:
    """Split settled picks by whether the sim agreed with the sharp line.

    The sim earns blend weight only if avg CLV on disagreements is positive:
    that is the subset where the sim claimed information the market lacked.
    """
    buckets: dict[str, list] = {"agreed": [], "disagreed": [], "no_sim": []}
    async with session_factory() as session:
        rows = (
            (await session.execute(select(Pick).where(Pick.clv.is_not(None))))
            .scalars()
            .all()
        )
    for pick in rows:
        if pick.sim_probability is None:
            buckets["no_sim"].append(pick)
        elif abs(pick.sim_probability - pick.sharp_probability) >= disagree_threshold:
            buckets["disagreed"].append(pick)
        else:
            buckets["agreed"].append(pick)

    def _summary(picks: list) -> dict:
        if not picks:
            return {"count": 0, "avg_clv": None, "profit_units": 0.0}
        return {
            "count": len(picks),
            "avg_clv": sum(p.clv for p in picks) / len(picks),
            "profit_units": sum(p.profit_units or 0.0 for p in picks),
        }

    return {name: _summary(picks) for name, picks in buckets.items()}
