"""SteamBot API routes.

GET  /health                  -- liveness check
POST /api/runs                -- start a picks run (triggers odds + pick agents)
GET  /api/runs/{run_id}       -- get run status and candidates (for HITL polling)
POST /api/runs/{run_id}/approve  -- submit approved pick IDs (resumes graph)
GET  /api/picks               -- list historical picks for CLV dashboard
POST /api/stripe/webhook      -- Stripe subscription events
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timezone

import stripe
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select

from langgraph.errors import GraphInterrupt

from steambot.api.main import get_graph, get_http_client
from steambot.db.models import Pick, User
from steambot.db.session import get_session_factory
from steambot.state import ApprovedPick, PickCandidate, SteamBotState

logger = logging.getLogger(__name__)

router = APIRouter()


class StartRunRequest(BaseModel):
    sport: str = "americanfootball_nfl"
    target_date: str = ""  # defaults to today
    user_id: str = "demo"


class StartRunResponse(BaseModel):
    run_id: str
    status: str
    message: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str  # "running" | "awaiting_review" | "complete" | "error"
    candidates: list[dict] = []
    approved_picks: list[dict] = []
    error: str | None = None


class ApprovePicksRequest(BaseModel):
    approved_pick_ids: list[str]
    user_id: str = "demo"


class PickRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    run_id: str
    game_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    market: str
    selection: str
    book: str
    price: int
    sharp_probability: float
    blended_probability: float
    edge_pct: float
    ev_pct: float
    confidence: str
    rationale: str
    approved_at: datetime
    closing_price: int | None = None
    closing_probability: float | None = None
    clv: float | None = None
    result: str | None = None
    profit_units: float | None = None


# In-memory run registry (per-instance; replace with DB for production).
_runs: dict[str, dict] = {}

# Stripe event ids we have already applied (per-instance; a redelivery or an
# out-of-order created/deleted pair would otherwise flip is_pro incorrectly).
# Mirrors the _runs per-instance limitation: replace with a DB table in production.
_processed_stripe_events: set[str] = set()


@router.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@router.post("/api/runs", response_model=StartRunResponse)
async def start_run(req: StartRunRequest):
    run_id = str(uuid.uuid4())
    target_date = req.target_date or date.today().isoformat()

    initial: SteamBotState = {
        "sport": req.sport,
        "target_date": target_date,
        "user_id": req.user_id,
        "games": [],
        "fair_lines": [],
        "candidates": [],
        "approved_picks": [],
        "bet_slips": [],
        "run_id": run_id,
        "error": None,
    }

    _runs[run_id] = {"status": "running", "state": initial, "owner": req.user_id}
    graph = get_graph()
    config = {
        "configurable": {"thread_id": run_id},
        "run_name": f"picks/{req.sport}/{target_date}",
        "metadata": {
            "run_id": run_id,
            "user_id": req.user_id,
            "sport": req.sport,
            "target_date": target_date,
        },
    }

    # Run graph asynchronously -- in production, offload to a background worker.
    try:
        result = await graph.ainvoke(initial, config=config)
        if result.get("error"):
            _runs[run_id]["status"] = "error"
            _runs[run_id]["error"] = result["error"]
        elif result.get("candidates"):
            _runs[run_id]["status"] = "awaiting_review"
            _runs[run_id]["state"] = result
        else:
            _runs[run_id]["status"] = "complete"
            _runs[run_id]["state"] = result
    except GraphInterrupt:
        _runs[run_id]["status"] = "awaiting_review"
    except Exception:
        logger.exception("start_run: graph invocation failed for run_id=%s", run_id)
        _runs[run_id]["status"] = "error"
        _runs[run_id]["error"] = "Run failed. See server logs."

    return StartRunResponse(
        run_id=run_id,
        status=_runs[run_id]["status"],
        message=f"Run {run_id} started for {target_date}",
    )


@router.get("/api/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str, user_id: str = "demo"):
    run = _runs.get(run_id)
    # Return 404 for both missing and non-owned runs so a caller cannot probe run_ids
    # they do not own. user_id is client-supplied today; production needs a real principal.
    if not run or run.get("owner") != user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    graph = get_graph()
    config = {"configurable": {"thread_id": run_id}}

    try:
        state = await graph.aget_state(config)
        candidates = [c.model_dump() if hasattr(c, "model_dump") else c for c in (state.values.get("candidates") or [])]
        approved = [p.model_dump() if hasattr(p, "model_dump") else p for p in (state.values.get("approved_picks") or [])]
        return RunStatusResponse(
            run_id=run_id,
            status=run["status"],
            candidates=candidates,
            approved_picks=approved,
            error=state.values.get("error"),
        )
    except Exception as exc:
        logger.warning("get_run: could not fetch graph state for run_id=%s: %s", run_id, exc)
        return RunStatusResponse(
            run_id=run_id,
            status=run.get("status", "unknown"),
            error=run.get("error"),
        )


@router.post("/api/runs/{run_id}/approve", response_model=RunStatusResponse)
async def approve_picks(run_id: str, req: ApprovePicksRequest):
    run = _runs.get(run_id)
    if not run or run.get("owner") != req.user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"Run is {run['status']}, not awaiting_review")

    graph = get_graph()
    config = {
        "configurable": {"thread_id": run_id},
        "run_name": f"approve/{run_id[:8]}",
        "metadata": {
            "run_id": run_id,
            "user_id": req.user_id,
            "approved_count": len(req.approved_pick_ids),
        },
    }

    from langgraph.types import Command

    try:
        result = await graph.ainvoke(
            Command(resume=req.approved_pick_ids),
            config=config,
        )
        _runs[run_id]["status"] = "complete"
        _runs[run_id]["state"] = result
        approved = [p.model_dump() if hasattr(p, "model_dump") else p for p in (result.get("approved_picks") or [])]
        return RunStatusResponse(run_id=run_id, status="complete", approved_picks=approved)
    except Exception:
        logger.exception("approve_picks: resume failed for run_id=%s", run_id)
        _runs[run_id]["status"] = "error"
        _runs[run_id]["error"] = "Approval failed. See server logs."
        raise HTTPException(status_code=500, detail="Approval failed")


@router.get("/api/picks", response_model=list[PickRecord])
async def list_picks(user_id: str, limit: int = Query(50, ge=1, le=200)):
    """Return approved picks for a user, ordered by approval time, newest first.

    user_id is caller-supplied with no JWT verification (same as all run
    endpoints). Adding auth is the first production-readiness gap; see
    Known Limitations in README.

    Includes CLV columns once settlement has populated them. Useful for
    verifying long-run edge: SELECT AVG(clv) WHERE result IS NOT NULL.
    """
    try:
        factory = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    async with factory() as session:
        q = (
            select(Pick)
            .where(Pick.user_id == user_id)
            .order_by(desc(Pick.approved_at))
            .limit(limit)
        )
        result = await session.execute(q)
        picks = result.scalars().all()

    return [PickRecord.model_validate(p) for p in picks]


@router.post("/api/stripe/webhook", status_code=200)
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_id = event.get("id")
    if event_id and event_id in _processed_stripe_events:
        return {"received": True, "duplicate": True}

    customer_id: str | None = None
    is_pro_grant: bool | None = None
    if event["type"] == "customer.subscription.created":
        customer_id = event.get("data", {}).get("object", {}).get("customer")
        is_pro_grant = True
    elif event["type"] == "customer.subscription.deleted":
        customer_id = event.get("data", {}).get("object", {}).get("customer")
        is_pro_grant = False

    if customer_id and is_pro_grant is not None:
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(User).where(User.stripe_customer_id == customer_id)
            )
            user = result.scalar_one_or_none()
            if user:
                user.is_pro = is_pro_grant
                await session.commit()
            else:
                logger.warning(
                    "stripe: no user found for customer_id=%r on event=%s",
                    customer_id,
                    event["type"],
                )

    if event_id:
        _processed_stripe_events.add(event_id)

    return {"received": True}
