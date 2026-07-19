"""Integration tests for the MLB schedule client using respx HTTP mocking."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from fairline.clients.mlb_schedule_client import fetch_probable_pitchers, resolve_probable_pitcher

_BASE = "https://statsapi.mlb.com"

_SCHEDULE_FIXTURE = {
    "dates": [
        {
            "games": [
                {
                    "gameDate": "2026-07-19T16:15:00Z",
                    "teams": {
                        "home": {
                            "team": {"name": "Toronto Blue Jays"},
                            "probablePitcher": {"fullName": "Trey Yesavage"},
                        },
                        "away": {
                            "team": {"name": "Chicago White Sox"},
                            "probablePitcher": {"fullName": "Sean Burke"},
                        },
                    },
                },
                {
                    "gameDate": "2026-07-19T20:07:00Z",
                    "teams": {
                        "home": {"team": {"name": "Los Angeles Angels"}},
                        "away": {"team": {"name": "Detroit Tigers"}},
                    },
                },
            ]
        }
    ]
}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_probable_pitchers_happy_path():
    respx.get(f"{_BASE}/api/v1/schedule", params={"sportId": "1", "date": "2026-07-19", "hydrate": "probablePitcher"}).mock(
        return_value=httpx.Response(200, json=_SCHEDULE_FIXTURE)
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_probable_pitchers(client, "2026-07-19")

    assert len(games) == 2
    assert games[0]["home_team"] == "Toronto Blue Jays"
    assert games[0]["away_team"] == "Chicago White Sox"
    assert games[0]["commence_time"] == datetime(2026, 7, 19, 16, 15, tzinfo=timezone.utc)
    assert games[0]["home_pitcher"] == "Trey Yesavage"
    assert games[0]["away_pitcher"] == "Sean Burke"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_probable_pitchers_missing_probable_is_none():
    respx.get(f"{_BASE}/api/v1/schedule", params={"sportId": "1", "date": "2026-07-19", "hydrate": "probablePitcher"}).mock(
        return_value=httpx.Response(200, json=_SCHEDULE_FIXTURE)
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_probable_pitchers(client, "2026-07-19")

    # Angels/Tigers game has no probablePitcher key on either side yet
    # (common the day before a start is announced) -- must be None, not KeyError.
    assert games[1]["home_pitcher"] is None
    assert games[1]["away_pitcher"] is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_probable_pitchers_empty_date_returns_empty_list():
    respx.get(f"{_BASE}/api/v1/schedule", params={"sportId": "1", "date": "2026-01-01", "hydrate": "probablePitcher"}).mock(
        return_value=httpx.Response(200, json={"dates": []})
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_probable_pitchers(client, "2026-01-01")
    assert games == []


_DOUBLEHEADER_GAMES = [
    {
        "home_team": "New York Yankees", "away_team": "Los Angeles Dodgers",
        "commence_time": datetime(2026, 7, 19, 16, 35, tzinfo=timezone.utc),
        "home_pitcher": "Cam Schlittler", "away_pitcher": "Yoshinobu Yamamoto",
    },
    {
        "home_team": "New York Yankees", "away_team": "Los Angeles Dodgers",
        "commence_time": datetime(2026, 7, 19, 23, 20, tzinfo=timezone.utc),
        "home_pitcher": "Will Warren", "away_pitcher": "Tyler Glasnow",
    },
]


def test_resolve_probable_pitcher_returns_the_requested_side():
    pitcher = resolve_probable_pitcher(
        _DOUBLEHEADER_GAMES, "New York Yankees", "Los Angeles Dodgers",
        datetime(2026, 7, 19, 16, 35, tzinfo=timezone.utc), "home",
    )
    assert pitcher == "Cam Schlittler"


def test_resolve_probable_pitcher_disambiguates_doubleheader_by_closest_time():
    # A snapshot commence_time close to the second game of the doubleheader
    # must resolve to that game's starters, not the first game's.
    pitcher = resolve_probable_pitcher(
        _DOUBLEHEADER_GAMES, "New York Yankees", "Los Angeles Dodgers",
        datetime(2026, 7, 19, 23, 15, tzinfo=timezone.utc), "away",
    )
    assert pitcher == "Tyler Glasnow"


def test_resolve_probable_pitcher_returns_none_with_no_matching_game():
    pitcher = resolve_probable_pitcher(
        _DOUBLEHEADER_GAMES, "Boston Red Sox", "Tampa Bay Rays",
        datetime(2026, 7, 19, 17, 35, tzinfo=timezone.utc), "home",
    )
    assert pitcher is None


def test_resolve_probable_pitcher_returns_none_when_pitcher_not_yet_announced():
    games = [{
        "home_team": "Los Angeles Angels", "away_team": "Detroit Tigers",
        "commence_time": datetime(2026, 7, 19, 20, 7, tzinfo=timezone.utc),
        "home_pitcher": None, "away_pitcher": None,
    }]
    pitcher = resolve_probable_pitcher(
        games, "Los Angeles Angels", "Detroit Tigers",
        datetime(2026, 7, 19, 20, 7, tzinfo=timezone.utc), "home",
    )
    assert pitcher is None
