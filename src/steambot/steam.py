"""Line-history capture for steam detection.

Steam is a fast, decisive move at the sharp book. Seeing it requires history,
so `steambot watch` polls the odds feed in a window before kickoff and stores
per-book snapshots. Detection over these rows lands in a later phase; this
module only collects the raw material.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from steambot.clients.odds_api import RETAIL_BOOKS, SHARP_BOOKS
from steambot.db.models import LineSnapshot
from steambot.state import GameSnapshot

logger = logging.getLogger(__name__)

TRACKED_BOOKS = SHARP_BOOKS | RETAIL_BOOKS


def games_in_window(
    games: list[GameSnapshot], now: datetime, window_hours: float
) -> list[GameSnapshot]:
    """Games that have not kicked off and start within the window."""
    cutoff = now + timedelta(hours=window_hours)
    return [g for g in games if now < g.commence_time <= cutoff]


def snapshot_rows(games: list[GameSnapshot], captured_at: datetime) -> list[LineSnapshot]:
    """Flatten game snapshots into one row per tracked book/market/outcome."""
    rows = []
    for game in games:
        for bm in game.bookmakers:
            if bm.key not in TRACKED_BOOKS:
                continue
            for mkt in bm.markets:
                for o in mkt.outcomes:
                    rows.append(
                        LineSnapshot(
                            game_id=game.game_id,
                            book=bm.key,
                            market=mkt.key,
                            outcome=o.name,
                            price=o.price,
                            point=o.point,
                            captured_at=captured_at,
                        )
                    )
    return rows


async def record_snapshots(
    games: list[GameSnapshot], session_factory, captured_at: datetime
) -> int:
    """Store one polling cycle's lines. Returns the number of rows written."""
    rows = snapshot_rows(games, captured_at)
    if not rows:
        return 0
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
    logger.info(
        "watch: stored %d line rows across %d games at %s",
        len(rows),
        len(games),
        captured_at.isoformat(timespec="seconds"),
    )
    return len(rows)
