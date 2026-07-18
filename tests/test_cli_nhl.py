"""CLI wiring test: --sport icehockey_nhl routes to the NHL matchup path."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_matchup_dispatches_to_nhl_path_for_nhl_sport(monkeypatch):
    import fairline.__main__ as cli_module

    called_with = {}

    async def fake_create_nhl_matchup_candidates(session_factory, snapshot, min_edge):
        called_with["snapshot_sport"] = snapshot.sport
        return 1

    monkeypatch.setattr(
        "fairline.nhl_matchup.create_nhl_matchup_candidates", fake_create_nhl_matchup_candidates
    )

    async def fake_fetch_odds(client, sport):
        from datetime import datetime, timedelta, timezone

        from fairline.state import GameSnapshot

        return [
            GameSnapshot(
                game_id="g1", sport=sport, home_team="Edmonton Oilers",
                away_team="Calgary Flames",
                commence_time=datetime.now(timezone.utc) + timedelta(hours=2),
                bookmakers=[],
            )
        ]

    async def fake_fetch_event_props(client, sport, game_id, markets):
        from datetime import datetime, timezone

        from fairline.state import GameSnapshot

        return GameSnapshot(
            game_id=game_id, sport=sport, home_team="Edmonton Oilers",
            away_team="Calgary Flames", commence_time=datetime.now(timezone.utc), bookmakers=[],
        )

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr("fairline.clients.odds_api.fetch_odds", fake_fetch_odds)
    monkeypatch.setattr("fairline.clients.odds_api.fetch_event_props", fake_fetch_event_props)

    await cli_module._matchup("icehockey_nhl", "player_points", 0.03, 5)

    assert called_with.get("snapshot_sport") == "icehockey_nhl"
