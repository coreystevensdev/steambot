"""Fairline LangGraph state machine.

Topology:
  odds_agent
      |
      v (error?) --> END
  trends_agent
      |
      v
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

from fairline.agents.odds import odds_agent
from fairline.agents.pick import pick_agent
from fairline.agents.validate import validate_agent
from fairline.state import ApprovedPick, FairlineState
from fairline.trends import trends_agent


async def _hitl_review(state: FairlineState) -> dict:
    """HITL checkpoint: pause graph, surface candidates to the user for approval.

    The API layer calls graph.invoke({...}, config={"thread_id": ...}) to start
    the run, which pauses here. The client resumes with approved_picks injected
    via graph.invoke(Command(resume={"approved_picks": [...]}), config=...).
    """
    candidates = state.get("candidates", [])
    if not candidates:
        return {"approved_picks": []}

    approved_ids: list[str] = interrupt(
        {
            "action": "review_picks",
            "candidates": [c.model_dump() for c in candidates],
        }
    )

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


def _route_after_odds(state: FairlineState) -> str:
    if state.get("error"):
        return END
    return "trends_agent"


def build_graph(client: httpx.AsyncClient, session_factory=None, checkpointer=None) -> StateGraph:
    """Compile and return the Fairline LangGraph.

    Pass a shared httpx.AsyncClient so nodes that make HTTP calls share a pool.
    Pass session_factory for validate_agent to write picks to the DB.
    Pass a checkpointer (MemorySaver or PostgresSaver) for HITL persistence.
    """
    g = StateGraph(FairlineState)

    g.add_node("odds_agent", partial(odds_agent, client=client))
    g.add_node("trends_agent", partial(trends_agent, session_factory=session_factory))
    g.add_node("pick_agent", pick_agent)
    g.add_node("hitl_review", _hitl_review)
    g.add_node("validate_agent", partial(validate_agent, session_factory=session_factory))

    g.set_entry_point("odds_agent")
    g.add_conditional_edges("odds_agent", _route_after_odds)
    g.add_edge("trends_agent", "pick_agent")
    g.add_edge("pick_agent", "hitl_review")
    g.add_edge("hitl_review", "validate_agent")
    g.add_edge("validate_agent", END)

    cp = checkpointer or MemorySaver()
    return g.compile(checkpointer=cp)
