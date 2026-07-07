"""Integration tests for The Odds API client using respx HTTP mocking.

All tests intercept at the httpx transport layer so no real network calls
are made. ODDS_API_KEY is set to a fake value via monkeypatch.
"""

from __future__ import annotations

import pytest
import httpx
import respx

from steambot.clients.odds_api import fetch_nfl_odds, fetch_sports, _parse_game


_FAKE_KEY = "test-api-key"
_ODDS_BASE = "https://api.the-odds-api.com/v4"

_NFL_GAME_FIXTURE = {
    "id": "abc123",
    "sport_key": "americanfootball_nfl",
    "sport_title": "NFL",
    "commence_time": "2026-01-15T20:00:00Z",
    "home_team": "Kansas City Chiefs",
    "away_team": "Las Vegas Raiders",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "last_update": "2026-01-15T18:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Kansas City Chiefs", "price": -165},
                        {"name": "Las Vegas Raiders", "price": 148},
                    ],
                },
                {
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Kansas City Chiefs", "price": -110, "point": -3.5},
                        {"name": "Las Vegas Raiders", "price": -110, "point": 3.5},
                    ],
                },
            ],
        },
        {
            "key": "fanduel",
            "title": "FanDuel",
            "last_update": "2026-01-15T18:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Kansas City Chiefs", "price": -170},
                        {"name": "Las Vegas Raiders", "price": 145},
                    ],
                }
            ],
        },
    ],
}


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", _FAKE_KEY)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_nfl_odds_happy_path():
    respx.get(f"{_ODDS_BASE}/sports/americanfootball_nfl/odds/").mock(
        return_value=httpx.Response(200, json=[_NFL_GAME_FIXTURE])
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_nfl_odds(client)
    assert len(games) == 1
    game = games[0]
    assert game.game_id == "abc123"
    assert game.home_team == "Kansas City Chiefs"
    assert game.away_team == "Las Vegas Raiders"
    assert len(game.bookmakers) == 2


@pytest.mark.asyncio
@respx.mock
async def test_fetch_nfl_odds_empty_off_season():
    respx.get(f"{_ODDS_BASE}/sports/americanfootball_nfl/odds/").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_nfl_odds(client)
    assert games == []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_nfl_odds_quota_exceeded_raises():
    respx.get(f"{_ODDS_BASE}/sports/americanfootball_nfl/odds/").mock(
        return_value=httpx.Response(401, json={"message": "You are not subscribed..."})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_nfl_odds(client)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_sports_returns_list():
    respx.get(f"{_ODDS_BASE}/sports/").mock(
        return_value=httpx.Response(
            200,
            json=[{"key": "americanfootball_nfl", "title": "NFL", "active": True}],
        )
    )
    async with httpx.AsyncClient() as client:
        sports = await fetch_sports(client)
    assert len(sports) == 1
    assert sports[0]["key"] == "americanfootball_nfl"


def test_parse_game_maps_bookmakers():
    game = _parse_game(_NFL_GAME_FIXTURE)
    assert game.game_id == "abc123"
    assert len(game.bookmakers) == 2
    pinnacle = next(b for b in game.bookmakers if b.key == "pinnacle")
    assert len(pinnacle.markets) == 2


def test_parse_game_spread_has_point():
    game = _parse_game(_NFL_GAME_FIXTURE)
    pinnacle = next(b for b in game.bookmakers if b.key == "pinnacle")
    spreads = next(m for m in pinnacle.markets if m.key == "spreads")
    chiefs = next(o for o in spreads.outcomes if "Chiefs" in o.name)
    assert chiefs.point == -3.5
    assert chiefs.price == -110


def test_parse_game_h2h_no_point():
    game = _parse_game(_NFL_GAME_FIXTURE)
    fanduel = next(b for b in game.bookmakers if b.key == "fanduel")
    h2h = next(m for m in fanduel.markets if m.key == "h2h")
    assert all(o.point is None for o in h2h.outcomes)


def test_parse_game_missing_required_field_returns_none():
    bad = {k: v for k, v in _NFL_GAME_FIXTURE.items() if k != "home_team"}
    assert _parse_game(bad) is None


def test_parse_game_skips_malformed_outcome():
    fixture = {
        "id": "g1",
        "sport_key": "americanfootball_nfl",
        "commence_time": "2026-01-15T20:00:00Z",
        "home_team": "A",
        "away_team": "B",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "A", "price": -110},
                            {"name": "B"},  # missing price, must be skipped
                        ],
                    }
                ],
            }
        ],
    }
    game = _parse_game(fixture)
    assert game is not None
    h2h = game.bookmakers[0].markets[0]
    assert len(h2h.outcomes) == 1
    assert h2h.outcomes[0].name == "A"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_nfl_odds_skips_malformed_game():
    malformed = {"id": "broken", "bookmakers": []}  # no home/away/commence_time
    respx.get(f"{_ODDS_BASE}/sports/americanfootball_nfl/odds/").mock(
        return_value=httpx.Response(200, json=[malformed, _NFL_GAME_FIXTURE])
    )
    async with httpx.AsyncClient() as client:
        games = await fetch_nfl_odds(client)
    assert len(games) == 1
    assert games[0].game_id == "abc123"
