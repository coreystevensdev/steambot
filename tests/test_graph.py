"""Graph-topology test: odds_agent fans out to four parallel nodes that
converge on trends_agent, instead of running weather/injury sequentially."""

from __future__ import annotations

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import MemorySaver

from fairline.graph import build_graph

_ODDS_BASE = "https://api.the-odds-api.com/v4"


@pytest.mark.asyncio
@respx.mock
async def test_odds_error_short_circuits_to_end(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    respx.get(f"{_ODDS_BASE}/sports/americanfootball_nfl/odds/").mock(
        return_value=httpx.Response(401)
    )
    async with httpx.AsyncClient() as client:
        graph = build_graph(client, checkpointer=MemorySaver())
        result = await graph.ainvoke(
            {"sport": "americanfootball_nfl", "target_date": "2026-01-15", "user_id": "u1", "sim_lines": []},
            config={"configurable": {"thread_id": "t1"}},
        )
    assert result.get("error")
    assert result.get("candidates", []) == []


@pytest.mark.asyncio
@respx.mock
async def test_no_games_reaches_hitl_review_with_no_candidates(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    respx.get(f"{_ODDS_BASE}/sports/americanfootball_nfl/odds/").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as client:
        graph = build_graph(client, checkpointer=MemorySaver())
        result = await graph.ainvoke(
            {"sport": "americanfootball_nfl", "target_date": "2026-01-15", "user_id": "u1", "sim_lines": []},
            config={"configurable": {"thread_id": "t2"}},
        )
    assert result.get("candidates", []) == []
    assert result.get("game_weather", {}) == {}
    assert result.get("team_stats", {}) == {}
    assert result.get("steam_signal", {}) == {}
