"""Trend records from Fairline's own data: SU, ATS, and O/U by team.

The inputs are already collected: `fairline watch` stores near-kickoff lines
for every game in its window, and the scores feed provides finals. Joining
them gives each game a closing spread and total, which is all ATS and O/U
records need. No stats provider involved.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_, select

from fairline.clients.odds_api import SHARP_BOOKS
from fairline.db.models import GameResult, LineSnapshot
from fairline.state import GameScore, FairlineState

logger = logging.getLogger(__name__)


def compute_team_trends(results: list[GameResult], team: str, last_n: int = 10) -> dict:
    """SU, ATS, and O/U records over the team's most recent games.

    ATS is graded from the team's perspective: the home closing spread flips
    sign for the away side, cover means margin plus the taken points beats
    zero, and exact landings push. Games with no stored line count toward SU
    only, so the three denominators can differ.
    """
    played = [
        r
        for r in results
        if team in (r.home_team, r.away_team)
        and r.home_score is not None
        and r.away_score is not None
    ]
    played.sort(key=lambda r: (r.commence_time is None, r.commence_time), reverse=True)
    window = played[:last_n]

    su = [0, 0, 0]
    ats = [0, 0, 0]
    ou = [0, 0, 0]

    def tally(bucket: list[int], diff: float) -> None:
        if diff > 0:
            bucket[0] += 1
        elif diff < 0:
            bucket[1] += 1
        else:
            bucket[2] += 1

    for r in window:
        is_home = r.home_team == team
        team_score = r.home_score if is_home else r.away_score
        opp_score = r.away_score if is_home else r.home_score
        margin = team_score - opp_score
        tally(su, margin)
        if r.closing_spread_home is not None:
            point = r.closing_spread_home if is_home else -r.closing_spread_home
            tally(ats, margin + point)
        if r.closing_total is not None:
            tally(ou, (r.home_score + r.away_score) - r.closing_total)

    def fmt(b: list[int]) -> str:
        return f"{b[0]}-{b[1]}-{b[2]}"

    return {"su": fmt(su), "ats": fmt(ats), "ou": fmt(ou), "n": len(window)}


async def _latest_closing_point(session, game_id: str, market: str, outcome: str) -> float | None:
    row = (
        (
            await session.execute(
                select(LineSnapshot)
                .where(
                    LineSnapshot.game_id == game_id,
                    LineSnapshot.market == market,
                    LineSnapshot.outcome == outcome,
                    LineSnapshot.book.in_(SHARP_BOOKS),
                )
                .order_by(LineSnapshot.captured_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return row.point if row else None


async def record_game_results(
    scores: list[GameScore], session_factory, sport: str
) -> int:
    """Join final scores with the last stored sharp line and upsert game_results.

    merge keeps re-runs harmless; grading and result capture share a cron.
    Games the watcher never saw get scores with NULL lines and count toward
    SU records only.
    """
    written = 0
    async with session_factory() as session:
        for s in scores:
            if not s.completed or s.home_score is None or s.away_score is None:
                continue
            spread = await _latest_closing_point(session, s.game_id, "spreads", s.home_team)
            total = await _latest_closing_point(session, s.game_id, "totals", "Over")
            await session.merge(
                GameResult(
                    game_id=s.game_id,
                    sport=sport,
                    home_team=s.home_team,
                    away_team=s.away_team,
                    commence_time=s.commence_time,
                    home_score=s.home_score,
                    away_score=s.away_score,
                    closing_spread_home=spread,
                    closing_total=total,
                )
            )
            written += 1
        await session.commit()
    if written:
        logger.info("trends: recorded %d game results for %s", written, sport)
    return written


async def trends_agent(state: FairlineState, session_factory=None) -> dict:
    """Attach recent SU/ATS/O-U records for every team in today's slate."""
    games = state.get("games", [])
    if not games or session_factory is None:
        return {"team_trends": {}}

    teams = {t for g in games for t in (g.home_team, g.away_team)}
    sport = state.get("sport", "americanfootball_nfl")
    async with session_factory() as session:
        results = (
            (
                await session.execute(
                    select(GameResult).where(
                        GameResult.sport == sport,
                        or_(
                            GameResult.home_team.in_(teams),
                            GameResult.away_team.in_(teams),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

    trends = {team: compute_team_trends(results, team) for team in teams}
    trends = {team: t for team, t in trends.items() if t["n"] > 0}
    logger.info("trends_agent: records for %d of %d teams", len(trends), len(teams))
    return {"team_trends": trends}
