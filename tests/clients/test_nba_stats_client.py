"""Tests for the NBA stats client: retry/backoff, timeout, and proxy pass-through.
No live nba_api calls in tests -- the nba_api call itself is monkeypatched."""

from __future__ import annotations

import pytest

from fairline.clients.nba_stats_client import fetch_league_game_log


class _FakeDataFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self._rows


class _FakeLeagueGameLog:
    def __init__(self, rows, fail_times=0):
        self._rows = rows
        self._fail_times = fail_times
        self._calls = 0

    def __call__(self, *args, **kwargs):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise ConnectionError("simulated stats.nba.com block")
        return self

    def get_data_frames(self):
        return [_FakeDataFrame(self._rows)]


@pytest.mark.asyncio
async def test_fetch_league_game_log_happy_path(monkeypatch):
    fake = _FakeLeagueGameLog(rows=[{"PLAYER_NAME": "LeBron James", "PTS": 28}])
    monkeypatch.setattr("fairline.clients.nba_stats_client._call_league_game_log", fake)

    rows = await fetch_league_game_log("2024-25")

    assert rows == [{"PLAYER_NAME": "LeBron James", "PTS": 28}]


@pytest.mark.asyncio
async def test_fetch_league_game_log_retries_on_failure(monkeypatch):
    fake = _FakeLeagueGameLog(rows=[{"PLAYER_NAME": "LeBron James", "PTS": 28}], fail_times=2)
    monkeypatch.setattr("fairline.clients.nba_stats_client._call_league_game_log", fake)
    monkeypatch.setattr("fairline.clients.nba_stats_client.time.sleep", lambda seconds: None)

    rows = await fetch_league_game_log("2024-25", max_retries=3)

    assert rows == [{"PLAYER_NAME": "LeBron James", "PTS": 28}]
    assert fake._calls == 3


@pytest.mark.asyncio
async def test_fetch_league_game_log_raises_after_exhausting_retries(monkeypatch):
    fake = _FakeLeagueGameLog(rows=[], fail_times=5)
    monkeypatch.setattr("fairline.clients.nba_stats_client._call_league_game_log", fake)
    monkeypatch.setattr("fairline.clients.nba_stats_client.time.sleep", lambda seconds: None)

    with pytest.raises(ConnectionError):
        await fetch_league_game_log("2024-25", max_retries=3)
