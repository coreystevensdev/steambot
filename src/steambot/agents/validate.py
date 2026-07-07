"""ValidationAgent node.

Records approved picks to the database for CLV and performance tracking.
CLV (Closing Line Value) = closing_prob - bet_prob. Positive CLV means the
bettor beat the closing line -- the strongest indicator of long-term edge.
"""

from __future__ import annotations

import logging

from steambot.state import SteamBotState

logger = logging.getLogger(__name__)


async def validate_agent(state: SteamBotState, session_factory=None) -> dict:
    """Persist approved picks to the picks table for CLV tracking."""
    approved = state.get("approved_picks", [])
    if not approved:
        logger.info("validate_agent: no approved picks to record")
        return {}

    if session_factory is None:
        logger.warning("validate_agent: no session factory configured, skipping DB write")
        return {}

    from steambot.db.models import Pick

    run_id = state.get("run_id", "")
    async with session_factory() as session:
        for ap in approved:
            pick = Pick(
                id=ap.pick.pick_id,
                user_id=ap.user_id,
                run_id=run_id,
                game_id=ap.pick.game_id,
                home_team=ap.pick.home_team,
                away_team=ap.pick.away_team,
                commence_time=ap.pick.commence_time,
                market=ap.pick.market,
                selection=ap.pick.selection,
                book=ap.pick.best_book,
                price=ap.pick.best_price,
                sharp_probability=ap.pick.sharp_probability,
                sim_probability=ap.pick.sim_probability,
                blended_probability=ap.pick.blended_probability,
                edge_pct=ap.pick.edge_pct,
                ev_pct=ap.pick.ev_pct,
                confidence=ap.pick.confidence,
                rationale=ap.pick.rationale,
                approved_at=ap.approved_at,
            )
            # merge, not add: a crash between DB commit and checkpoint save makes
            # LangGraph re-run this node on resume with the same pick ids
            await session.merge(pick)
            logger.info(
                "validate_agent: recording pick_id=%s selection=%r edge=%.1f%% confidence=%s",
                ap.pick.pick_id,
                ap.pick.selection,
                ap.pick.edge_pct * 100,
                ap.pick.confidence,
            )
        await session.commit()

    return {}
