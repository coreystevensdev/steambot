"""Steam-move signal for pick_agent: wraps the detector fairline already has.

`fairline watch` stores line history and `scan_recent_steam` already detects
sharp moves for the standalone steam-candidate review queue (see
`fairline.steam.create_steam_candidates`). This node runs the same detector
inside the main graph so pick_agent's moneyline/spread/total picks get
"sharp money just moved here" as one more input, without a second
implementation of steam detection.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from fairline.state import FairlineState
from fairline.steam import format_steam_event, scan_recent_steam

logger = logging.getLogger(__name__)


async def signal_agent(state: FairlineState, session_factory=None) -> dict:
    """Attach recent steam-move events per game_id, formatted for the pick prompt."""
    if session_factory is None:
        return {"steam_signal": {}}

    try:
        events = await scan_recent_steam(session_factory)
    except SQLAlchemyError as exc:
        logger.warning("signal_agent: steam scan failed: %s", exc)
        return {"steam_signal": {}}

    signal: dict[str, list[str]] = {}
    for event in events:
        signal.setdefault(event.game_id, []).append(format_steam_event(event))

    logger.info("signal_agent: steam signal for %d games", len(signal))
    return {"steam_signal": signal}
