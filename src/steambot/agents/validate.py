"""ValidationAgent node.

Records approved picks to the database for CLV and performance tracking.
CLV (Closing Line Value) = closing_prob - bet_prob. Positive CLV means the
bettor beat the closing line -- the strongest indicator of long-term edge.
"""

from __future__ import annotations

import logging
from datetime import datetime

from steambot.state import ApprovedPick, SteamBotState

logger = logging.getLogger(__name__)


async def validate_agent(state: SteamBotState) -> dict:
    """Persist approved picks for CLV tracking. No external I/O beyond the DB."""
    approved = state.get("approved_picks", [])
    if not approved:
        logger.info("validate_agent: no approved picks to record")
        return {}

    for ap in approved:
        logger.info(
            "validate_agent: recording pick_id=%s selection=%r edge=%.1f%% confidence=%s",
            ap.pick.pick_id,
            ap.pick.selection,
            ap.pick.edge_pct * 100,
            ap.pick.confidence,
        )
        # DB write happens in the API layer via the picks table.
        # This node is a hook for post-approval side-effects (notifications, etc.).

    return {}
