"""CLI wiring test: --sport basketball_nba routes to the NBA matchup path."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_matchup_dispatches_to_nba_path_for_nba_sport(monkeypatch):
    import fairline.__main__ as cli_module

    called_with = {}

    async def fake_create_nba_matchup_candidates(session_factory, snapshot, min_edge):
        called_with["snapshot_sport"] = snapshot.sport
        return 1

    monkeypatch.setattr(
        "fairline.nba_matchup.create_nba_matchup_candidates", fake_create_nba_matchup_candidates
    )

    async def fake_fetch_odds(client, sport):
        from datetime import datetime, timedelta, timezone

        from fairline.state import GameSnapshot

        return [
            GameSnapshot(
                game_id="g1", sport=sport, home_team="Los Angeles Lakers",
                away_team="Boston Celtics",
                commence_time=datetime.now(timezone.utc) + timedelta(hours=2),
                bookmakers=[],
            )
        ]

    async def fake_fetch_event_props(client, sport, game_id, markets):
        from datetime import datetime, timezone

        from fairline.state import GameSnapshot

        return GameSnapshot(
            game_id=game_id, sport=sport, home_team="Los Angeles Lakers",
            away_team="Boston Celtics", commence_time=datetime.now(timezone.utc), bookmakers=[],
        )

    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr("fairline.clients.odds_api.fetch_odds", fake_fetch_odds)
    monkeypatch.setattr("fairline.clients.odds_api.fetch_event_props", fake_fetch_event_props)

    await cli_module._matchup("basketball_nba", "player_points", 0.03, 5)

    assert called_with.get("snapshot_sport") == "basketball_nba"


@pytest.mark.asyncio
async def test_backfill_nba_players_persists_position(monkeypatch):
    import fairline.__main__ as cli_module
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from fairline.db.models import Base, NbaPlayerGame

    async def fake_fetch(season, proxy=None):
        from datetime import datetime, timezone

        return [
            NbaPlayerGame(
                season=2025, game_date=datetime(2025, 12, 1, tzinfo=timezone.utc),
                player="LeBron James", team="Los Angeles Lakers", opponent="Boston Celtics",
                is_home=True, position="Forward", points=28, rebounds=8, assists=9, three_pointers_made=3,
            )
        ]

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr("fairline.nba_stats.fetch_nba_player_games", fake_fetch)
    monkeypatch.setattr("fairline.db.session.get_session_factory", lambda: factory)

    await cli_module._backfill_nba_players("2024-25", None)

    async with factory() as session:
        rows = (await session.execute(select(NbaPlayerGame))).scalars().all()
    assert [row.position for row in rows] == ["Forward"]

    await engine.dispose()
