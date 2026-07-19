"""NBA per-game player stats via nba_api's bulk LeagueGameLog endpoint.

stats.nba.com is documented to block requests from cloud/datacenter IPs
(confirmed during planning: a GitHub issue on nba_api reports Heroku
specifically being blocked, and community consensus is that free proxy
lists are already blacklisted). No free, reliable mitigation exists. This
client does the two things actually within reach: an explicit timeout and
retry (nba_api has neither built in, its default timeout is None and it
makes exactly one request with no backoff), plus a pass-through proxy
parameter (nba_api supports proxy=/headers=/timeout= natively on every
endpoint class since v1.1.0) so a deployment with a residential-IP relay
or a non-hyperscaler VPS can actually use this.

nba_api is synchronous (wraps requests), so every call runs via
asyncio.to_thread, the same pattern the MLB feature uses for pybaseball.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 2.0


def _call_league_game_log(season: str, proxy: str | None, timeout: float):
    from nba_api.stats.endpoints import leaguegamelog

    kwargs = {
        "season": season,
        "season_type_all_star": "Regular Season",
        "player_or_team_abbreviation": "P",
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return leaguegamelog.LeagueGameLog(**kwargs)


def _fetch_sync(season: str, proxy: str | None, timeout: float, max_retries: int) -> list[dict]:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            log = _call_league_game_log(season, proxy, timeout)
            return log.get_data_frames()[0].to_dict(orient="records")
        except Exception as exc:  # nba_api/requests raise varied, undocumented exception types on a block
            last_exc = exc
            if attempt < max_retries - 1:
                sleep_for = _BACKOFF_BASE_SECONDS * (2 ** attempt)
                logger.warning(
                    "nba_stats: attempt %d/%d failed (%s), retrying in %.0fs",
                    attempt + 1, max_retries, exc, sleep_for,
                )
                time.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


async def fetch_league_game_log(
    season: str, proxy: str | None = None, timeout: float = 60.0, max_retries: int = 3
) -> list[dict]:
    """Every player's per-game log for one NBA season, raw dict rows.

    `season` is nba_api's format, e.g. "2024-25". Raises whatever the final
    retry attempt raised if every attempt fails (network errors from a
    stats.nba.com block have no single documented exception type, so this
    catches broadly here specifically to drive the retry loop, then
    re-raises rather than swallowing the failure).
    """
    return await asyncio.to_thread(_fetch_sync, season, proxy, timeout, max_retries)
