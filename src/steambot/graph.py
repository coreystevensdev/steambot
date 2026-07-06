"""SteamBot LangGraph state machine.

Topology:
  odds_agent
      |
      v (error?) --> END
  pick_agent
      |
      v
  [HITL interrupt -- user reviews candidates in UI]
      |
      v (approved picks injected via graph.invoke resume)
  validate_agent
      |
      END

The HITL interrupt uses LangGraph's interrupt() primitive. The graph is
compiled with a checkpointer (MemorySaver for local dev; PostgresSaver for
production) so the paused state survives across HTTP requests.
"""

from __future__ import annotations

from functools import partial

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from steambot.agents.odds import odds_agent
from steambot.agents.pick import pick_agent
from steambot.agents.validate import validate_agent
from steambot.state import ApprovedPick, SteamBotState


async def _hitl_review(state: SteamBotState) -> dict:
    """HITL checkpoint: pause graph, surface candidates to the user for approval.

    The API layer calls graph.invoke({...}, config={"thread_id": ...}) to start
    the run, which pauses here. The client resumes with approved_picks injected
    via graph.invoke(Command(resume={"approved_picks": [...]}), config=...).
    """
    candidates = state.get("candidates", [])
    if not candidates:
        return {"approved_picks": []}

    # Pause the graph and surface the candidates to the UI.
    approved_ids: list[str] = interrupt(
        {
            "action": "review_picks",
            "candidates": [c.model_dump() for c in candidates],
        }
    )

    # After resume, build ApprovedPick objects from the approved candidate IDs.
    from datetime import datetime, timezone

    cand_map = {c.pick_id: c for c in candidates}
    approved_picks = [
        ApprovedPick(
            pick=cand_map[pid],
            approved_at=datetime.now(timezone.utc),
            user_id=state.get("user_id", "anonymous"),
        )
        for pid in approved_ids
        if pid in cand_map
    ]
    return {"approved_picks": approved_picks}


def _route_after_odds(state: SteamBotState) -> str:
    if state.get("error"):
        return END
    return "pick_agent"


def build_graph(client: httpx.AsyncClient, checkpointer=None) -> StateGraph:
    """Compile and return the SteamBot LangGraph.

    Pass a shared httpx.AsyncClient so nodes that make HTTP calls share a pool.
    Pass a checkpointer (MemorySaver or PostgresSaver) for HITL persistence.
    """
    g = StateGraph(SteamBotState)

    g.add_node("odds_agent", partial(odds_agent, client=client))
    g.add_node("pick_agent", pick_agent)
    g.add_node("hitl_review", _hitl_review)
    g.add_node("validate_agent", validate_agent)

    g.set_entry_point("odds_agent")
    g.add_conditional_edges("odds_agent", _route_after_odds)
    g.add_edge("pick_agent", "hitl_review")
    g.add_edge("hitl_review", "validate_agent")
    g.add_edge("validate_agent", END)

    cp = checkpointer or MemorySaver()
    return g.compile(checkpointer=cp)
