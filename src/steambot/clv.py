"""CLV settlement: capture closing lines for approved picks.

The free Odds API tier has no historical endpoint, so the closing line is the
last sharp-book snapshot taken near kickoff. Run `python -m steambot settle`
shortly before games start; picks whose window is missed keep clv NULL.

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
from steambot.state import GameSnapshot, american_to_prob, remove_vig

logger = logging.getLogger(__name__)


class ClosingLine(NamedTuple):
    price: int
    probability: float
    book: str


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
    return ClosingLine(price=mkt.outcomes[idx].price, probability=fair[idx], book=book)


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
