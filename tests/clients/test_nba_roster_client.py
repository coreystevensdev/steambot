"""Tests for the NBA roster/position client: bucketing, bulk fetch across all teams.
No live nba_api calls in tests -- team list and roster calls are monkeypatched."""

from __future__ import annotations

import pytest

from fairline.clients.nba_roster_client import _bucket_position, fetch_league_positions


class TestBucketPosition:
    def test_single_guard(self):
        assert _bucket_position("Guard") == "Guard"

    def test_single_forward(self):
        assert _bucket_position("Forward") == "Forward"

    def test_single_center(self):
        assert _bucket_position("Center") == "Center"

    def test_combo_string_takes_first_listed(self):
        assert _bucket_position("Guard-Forward") == "Guard"
        assert _bucket_position("Forward-Center") == "Forward"

    def test_unrecognized_value_returns_none(self):
        assert _bucket_position("") is None
        assert _bucket_position(None) is None

    def test_letter_code_guard(self):
        assert _bucket_position("G") == "Guard"

    def test_letter_code_forward(self):
        assert _bucket_position("F") == "Forward"

    def test_letter_code_center(self):
        assert _bucket_position("C") == "Center"

    def test_letter_code_combo_takes_first_listed(self):
        assert _bucket_position("G-F") == "Guard"
        assert _bucket_position("F-C") == "Forward"


@pytest.mark.asyncio
async def test_fetch_league_positions_covers_all_teams(monkeypatch):
    fake_teams = [{"id": 1}, {"id": 2}]

    def fake_get_teams():
        return fake_teams

    class _FakeRosterRow:
        def __init__(self, rows):
            self._rows = rows

        def to_dict(self, orient):
            assert orient == "records"
            return self._rows

    def fake_roster_call(team_id, season, proxy=None, timeout=60.0):
        rows = {
            1: [{"PLAYER": "LeBron James", "POSITION": "Forward"}],
            2: [{"PLAYER": "Nikola Jokic", "POSITION": "Center"}],
        }[team_id]
        return _FakeRosterRow(rows)

    monkeypatch.setattr("fairline.clients.nba_roster_client._get_teams", fake_get_teams)
    monkeypatch.setattr("fairline.clients.nba_roster_client._call_team_roster", fake_roster_call)

    positions = await fetch_league_positions("2024-25")

    assert positions == {"LeBron James": "Forward", "Nikola Jokic": "Center"}


@pytest.mark.asyncio
async def test_fetch_league_positions_skips_team_that_fails_all_retries(monkeypatch):
    fake_teams = [{"id": 1}, {"id": 2}]

    def fake_get_teams():
        return fake_teams

    class _FakeRosterRow:
        def __init__(self, rows):
            self._rows = rows

        def to_dict(self, orient):
            assert orient == "records"
            return self._rows

    def fake_roster_call(team_id, season, proxy=None, timeout=60.0):
        if team_id == 1:
            raise RuntimeError("stats.nba.com blocked the request")
        return _FakeRosterRow([{"PLAYER": "Nikola Jokic", "POSITION": "Center"}])

    monkeypatch.setattr("fairline.clients.nba_roster_client._get_teams", fake_get_teams)
    monkeypatch.setattr("fairline.clients.nba_roster_client._call_team_roster", fake_roster_call)
    monkeypatch.setattr("fairline.clients.nba_roster_client.time.sleep", lambda seconds: None)

    positions = await fetch_league_positions("2024-25", max_retries=2)

    assert positions == {"Nikola Jokic": "Center"}
