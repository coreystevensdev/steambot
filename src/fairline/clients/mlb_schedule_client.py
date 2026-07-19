"""MLB's own free official Stats API: no auth, no key, plain JSON.

Used only for probable starting pitchers on upcoming games -- historical
opposing-pitcher data for backfilled games comes from pybaseball's Statcast
pull instead (see mlb_stats.py). Verified live during planning (2026-07-19)
against a real schedule pull: team names come through as full names
("New York Yankees") matching GameSnapshot's format, and pitcher names come
through as "First Last" matching mlb_stats.py's own name format -- no
mapping table needed for either, unlike NFL's NFL_TEAMS code-to-name table.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

_BASE = "https://statsapi.mlb.com"


async def fetch_probable_pitchers(client: httpx.AsyncClient, date: str) -> list[dict]:
    """Every MLB game on `date` (YYYY-MM-DD) with its probable starters.

    Returns a list of {"home_team", "away_team", "commence_time",
    "home_pitcher", "away_pitcher"} -- the same two teams can appear twice
    in this list on a doubleheader date; disambiguating which row matches a
    specific snapshot is resolve_probable_pitcher's job, not this function's.
    A missing probablePitcher (common the day before a start is announced)
    comes back as None, never a KeyError.
    """
    resp = await client.get(
        f"{_BASE}/api/v1/schedule",
        params={"sportId": "1", "date": date, "hydrate": "probablePitcher"},
        timeout=httpx.Timeout(30.0),
    )
    resp.raise_for_status()
    payload = resp.json()

    games: list[dict] = []
    for sched_date in payload.get("dates") or []:
        for game in sched_date.get("games") or []:
            teams = game.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            try:
                commence_time = datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            games.append({
                "home_team": (home.get("team") or {}).get("name"),
                "away_team": (away.get("team") or {}).get("name"),
                "commence_time": commence_time.astimezone(timezone.utc),
                "home_pitcher": (home.get("probablePitcher") or {}).get("fullName"),
                "away_pitcher": (away.get("probablePitcher") or {}).get("fullName"),
            })
    return games


def resolve_probable_pitcher(
    games: list[dict], home_team: str, away_team: str, commence_time: datetime, side: str
) -> str | None:
    """The probable pitcher for `side` ("home" or "away") in the game matching
    `home_team`/`away_team` whose commence_time is closest to the snapshot's --
    the same two teams can appear twice on one date (a doubleheader), so a
    plain first-match would silently grab the wrong game's starter."""
    candidates = [g for g in games if g["home_team"] == home_team and g["away_team"] == away_team]
    if not candidates:
        return None
    closest = min(candidates, key=lambda g: abs((g["commence_time"] - commence_time).total_seconds()))
    return closest.get(f"{side}_pitcher")
