"""Steam detection: line-history capture and the move detector.

Steam is a fast, decisive move at the sharp book. Seeing it requires history,
so `fairline watch` polls the odds feed in a window before kickoff, stores
per-book snapshots, and flags sharp moves against the recent baseline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from fairline.clients.odds_api import RETAIL_BOOKS, SHARP_BOOKS
from fairline.db.models import LineSnapshot
from fairline.state import GameSnapshot, american_to_prob, remove_vig

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
                            sport=game.sport,
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


KEY_NUMBERS = (3.0, 7.0)  # most common NFL margins; NFL-only, gated by sport in detect_steam
DEFAULT_PROB_THRESHOLD = 0.02
DEFAULT_MAX_ELAPSED_SECONDS = 600.0


class SteamEvent(NamedTuple):
    game_id: str
    market: str
    outcome: str
    book: str
    prob_move: float
    new_prob: float
    old_price: int
    new_price: int
    old_point: float | None
    new_point: float | None
    crossed_key: bool
    elapsed_seconds: float


def crossed_key_number(old_point: float | None, new_point: float | None) -> bool:
    """True when a spread move lands on or passes through 3 or 7."""
    if old_point is None or new_point is None or old_point == new_point:
        return False
    lo, hi = sorted((abs(old_point), abs(new_point)))
    return any(lo <= k <= hi for k in KEY_NUMBERS)


def _devig_group(rows: list[LineSnapshot]) -> dict[str, float]:
    fair = remove_vig([american_to_prob(r.price) for r in rows])
    return {r.outcome: p for r, p in zip(rows, fair)}


def detect_steam(
    old_rows: list[LineSnapshot],
    new_rows: list[LineSnapshot],
    prob_threshold: float = DEFAULT_PROB_THRESHOLD,
    max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
) -> list[SteamEvent]:
    """Compare two snapshot cycles of the sharp book and flag decisive moves.

    Fires on a no-vig probability jump of prob_threshold or more, or on a
    spread crossing a key number toward the favorite. Slow drift is not steam:
    baselines older than max_elapsed_seconds produce no events.
    """

    def by_group(rows):
        groups: dict[tuple[str, str, str], list[LineSnapshot]] = {}
        for r in rows:
            groups.setdefault((r.game_id, r.market, r.book), []).append(r)
        return groups

    events: list[SteamEvent] = []
    old_groups, new_groups = by_group(old_rows), by_group(new_rows)
    for key, new_group in new_groups.items():
        old_group = old_groups.get(key)
        if not old_group or len(old_group) < 2 or len(new_group) < 2:
            continue
        elapsed = (new_group[0].captured_at - old_group[0].captured_at).total_seconds()
        if elapsed <= 0 or elapsed > max_elapsed_seconds:
            continue

        old_fair = _devig_group(old_group)
        new_fair = _devig_group(new_group)
        old_by_name = {r.outcome: r for r in old_group}
        for new_row in new_group:
            old_row = old_by_name.get(new_row.outcome)
            if old_row is None or new_row.outcome not in old_fair:
                continue
            prob_move = new_fair[new_row.outcome] - old_fair[new_row.outcome]
            key_cross = (
                new_row.sport == "americanfootball_nfl"
                and new_row.market == "spreads"
                and crossed_key_number(old_row.point, new_row.point)
                and new_row.point is not None
                and old_row.point is not None
                and new_row.point < old_row.point
            )
            if prob_move >= prob_threshold or key_cross:
                events.append(
                    SteamEvent(
                        game_id=new_row.game_id,
                        market=new_row.market,
                        outcome=new_row.outcome,
                        book=new_row.book,
                        prob_move=prob_move,
                        new_prob=new_fair[new_row.outcome],
                        old_price=old_row.price,
                        new_price=new_row.price,
                        old_point=old_row.point,
                        new_point=new_row.point,
                        crossed_key=key_cross,
                        elapsed_seconds=elapsed,
                    )
                )
    return events


async def scan_recent_steam(
    session_factory,
    lookback_minutes: float = 12.0,
    prob_threshold: float = DEFAULT_PROB_THRESHOLD,
) -> list[SteamEvent]:
    """Compare the newest sharp-book cycle against the oldest one inside the lookback.

    Cron-collected cycles do not land on a neat grid, so the baseline is
    "oldest stamp within the window", not "exactly N minutes ago".
    """
    from sqlalchemy import select

    async with session_factory() as session:
        stamps = (
            (
                await session.execute(
                    select(LineSnapshot.captured_at)
                    .where(LineSnapshot.book.in_(SHARP_BOOKS))
                    .distinct()
                    .order_by(LineSnapshot.captured_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if len(stamps) < 2:
            return []
        latest = stamps[0]
        window_floor = latest - timedelta(minutes=lookback_minutes)
        candidates = [s for s in stamps[1:] if s >= window_floor]
        if not candidates:
            return []
        baseline = min(candidates)

        async def rows_at(stamp):
            return (
                (
                    await session.execute(
                        select(LineSnapshot).where(
                            LineSnapshot.book.in_(SHARP_BOOKS),
                            LineSnapshot.captured_at == stamp,
                        )
                    )
                )
                .scalars()
                .all()
            )

        old_rows = await rows_at(baseline)
        new_rows = await rows_at(latest)

    max_elapsed = lookback_minutes * 60 + 60  # the window defines staleness here
    return detect_steam(old_rows, new_rows, prob_threshold, max_elapsed_seconds=max_elapsed)


def format_steam_event(e: SteamEvent) -> str:
    point_part = ""
    if e.old_point is not None:
        key_flag = " KEY" if e.crossed_key else ""
        point_part = f" point {e.old_point:+g} -> {e.new_point:+g}{key_flag}"
    return (
        f"STEAM {e.outcome} ({e.market}) {e.old_price:+d} -> {e.new_price:+d}"
        f"{point_part} prob {e.prob_move:+.3f} in {e.elapsed_seconds / 60:.0f}m via {e.book}"
    )


MIN_STEAM_EDGE = 0.02


def _confidence(edge: float) -> str:
    if edge > 0.05:
        return "high"
    if edge >= 0.03:
        return "medium"
    return "low"


async def create_steam_candidates(
    session_factory,
    events: list[SteamEvent],
    games: list[GameSnapshot],
    min_edge: float = MIN_STEAM_EDGE,
) -> int:
    """Turn steam events into pending candidates at retail books that lag.

    A book lags when its current implied probability sits at least min_edge
    below the sharp book's new no-vig number. One pending candidate per
    game/market/selection/book; re-detection of the same move is a no-op.
    """
    import uuid

    from sqlalchemy import select as sa_select

    from fairline.db.models import SteamCandidate

    games_by_id = {g.game_id: g for g in games}
    created = 0
    async with session_factory() as session:
        for event in events:
            game = games_by_id.get(event.game_id)
            if game is None:
                continue
            latest_stamp = (
                await session.execute(
                    sa_select(LineSnapshot.captured_at)
                    .where(
                        LineSnapshot.game_id == event.game_id,
                        LineSnapshot.market == event.market,
                        LineSnapshot.book.in_(RETAIL_BOOKS),
                    )
                    .order_by(LineSnapshot.captured_at.desc())
                    .limit(1)
                )
            ).scalar()
            if latest_stamp is None:
                continue
            retail_rows = (
                (
                    await session.execute(
                        sa_select(LineSnapshot).where(
                            LineSnapshot.game_id == event.game_id,
                            LineSnapshot.market == event.market,
                            LineSnapshot.outcome == event.outcome,
                            LineSnapshot.book.in_(RETAIL_BOOKS),
                            LineSnapshot.captured_at == latest_stamp,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in retail_rows:
                implied = american_to_prob(row.price)
                edge = event.new_prob - implied
                if edge < min_edge:
                    continue
                selection = (
                    f"{event.outcome} {row.point:+g}" if row.point is not None else event.outcome
                )
                already_pending = (
                    await session.execute(
                        sa_select(SteamCandidate.id).where(
                            SteamCandidate.game_id == event.game_id,
                            SteamCandidate.market == event.market,
                            SteamCandidate.selection == selection,
                            SteamCandidate.book == row.book,
                            SteamCandidate.status == "pending",
                        )
                    )
                ).scalar()
                if already_pending:
                    continue
                win_amount = row.price / 100 if row.price > 0 else 100 / abs(row.price)
                session.add(
                    SteamCandidate(
                        id=str(uuid.uuid4()),
                        sport=game.sport,
                        game_id=event.game_id,
                        home_team=game.home_team,
                        away_team=game.away_team,
                        commence_time=game.commence_time,
                        market=event.market,
                        selection=selection,
                        book=row.book,
                        price=row.price,
                        sharp_probability=event.new_prob,
                        implied_probability=implied,
                        edge_pct=edge,
                        ev_pct=event.new_prob * win_amount - (1 - event.new_prob),
                        rationale=f"{format_steam_event(event)}; {row.book} stale at {row.price:+d}",
                        status="pending",
                    )
                )
                created += 1
        await session.commit()
    if created:
        logger.info("steam: %d candidates pending review", created)
    return created


async def approve_steam_candidate(session_factory, candidate_id: str, user_id: str) -> str | None:
    """Turn a pending candidate into a Pick with source='steam'. None if gone."""
    import uuid
    from datetime import timezone as _tz

    from sqlalchemy import select as sa_select

    from fairline.db.models import Pick, SteamCandidate

    async with session_factory() as session:
        cand = (
            await session.execute(sa_select(SteamCandidate).where(SteamCandidate.id == candidate_id))
        ).scalar_one_or_none()
        if cand is None or cand.status != "pending":
            return None
        pick_id = str(uuid.uuid4())
        session.add(
            Pick(
                id=pick_id,
                user_id=user_id,
                run_id=f"steam:{cand.id[:8]}",
                sport=cand.sport,
                game_id=cand.game_id,
                home_team=cand.home_team,
                away_team=cand.away_team,
                commence_time=cand.commence_time,
                market=cand.market,
                selection=cand.selection,
                book=cand.book,
                price=cand.price,
                sharp_probability=cand.sharp_probability,
                sim_probability=None,
                blended_probability=cand.sharp_probability,
                edge_pct=cand.edge_pct,
                ev_pct=cand.ev_pct,
                confidence=_confidence(cand.edge_pct),
                rationale=cand.rationale,
                source="steam",
                approved_at=datetime.now(_tz.utc),
            )
        )
        cand.status = "approved"
        await session.commit()
        return pick_id


async def reject_steam_candidate(session_factory, candidate_id: str) -> bool:
    from sqlalchemy import select as sa_select

    from fairline.db.models import SteamCandidate

    async with session_factory() as session:
        cand = (
            await session.execute(sa_select(SteamCandidate).where(SteamCandidate.id == candidate_id))
        ).scalar_one_or_none()
        if cand is None or cand.status != "pending":
            return False
        cand.status = "rejected"
        await session.commit()
        return True
