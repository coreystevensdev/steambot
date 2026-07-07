"""Fairline API routes.

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
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select

from langgraph.errors import GraphInterrupt

from fairline.api.auth import Principal, require_user
from fairline.api.main import get_graph, get_http_client
from fairline.runs import create_run, fetch_run, update_run
from fairline.db.models import Pick, User
from fairline.db.session import get_session_factory
from fairline.state import ApprovedPick, PickCandidate, SimLine, FairlineState

logger = logging.getLogger(__name__)

router = APIRouter()


class StartRunRequest(BaseModel):
    sport: str = "americanfootball_nfl"
    target_date: str = ""  # defaults to today
    # external simulation probabilities, matched to games by team names;
    # picks without a matching sim blend at weight 0 (sharp line only)
    sims: list[SimLine] = []


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


class PickRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    run_id: str
    sport: str
    source: str
    game_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    market: str
    selection: str
    book: str
    price: int
    sharp_probability: float
    sim_probability: float | None = None
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


def _registry_factory():
    try:
        return get_session_factory()
    except RuntimeError:
        return None


# Stripe event ids we have already applied (per-instance; a redelivery or an
# out-of-order created/deleted pair would otherwise flip is_pro incorrectly).
# Last remaining in-process registry; a DB table is the production answer.
_processed_stripe_events: set[str] = set()


@router.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@router.post("/api/runs", response_model=StartRunResponse)
async def start_run(req: StartRunRequest, user: Principal = Depends(require_user)):
    from fairline.clients.odds_api import SUPPORTED_SPORTS

    if req.sport not in SUPPORTED_SPORTS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported sport {req.sport!r}; supported: {sorted(SUPPORTED_SPORTS)}",
        )
    run_id = str(uuid.uuid4())
    target_date = req.target_date or date.today().isoformat()

    initial: FairlineState = {
        "sport": req.sport,
        "target_date": target_date,
        "user_id": user.user_id,
        "sim_lines": req.sims,
        "team_trends": {},
        "games": [],
        "fair_lines": [],
        "candidates": [],
        "approved_picks": [],
        "bet_slips": [],
        "run_id": run_id,
        "error": None,
    }

    registry = _registry_factory()
    await create_run(registry, run_id, user.user_id)
    graph = get_graph()
    config = {
        "configurable": {"thread_id": run_id},
        "run_name": f"picks/{req.sport}/{target_date}",
        "metadata": {
            "run_id": run_id,
            "user_id": user.user_id,
            "sport": req.sport,
            "target_date": target_date,
        },
    }

    # Run graph asynchronously -- in production, offload to a background worker.
    status = "running"
    try:
        result = await graph.ainvoke(initial, config=config)
        if result.get("error"):
            status = "error"
            await update_run(registry, run_id, status, error=result["error"])
        elif result.get("candidates"):
            status = "awaiting_review"
            await update_run(registry, run_id, status)
        else:
            status = "complete"
            await update_run(registry, run_id, status)
    except GraphInterrupt:
        status = "awaiting_review"
        await update_run(registry, run_id, status)
    except Exception:
        logger.exception("start_run: graph invocation failed for run_id=%s", run_id)
        status = "error"
        await update_run(registry, run_id, status, error="Run failed. See server logs.")

    return StartRunResponse(
        run_id=run_id,
        status=status,
        message=f"Run {run_id} started for {target_date}",
    )


@router.get("/api/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(run_id: str, user: Principal = Depends(require_user)):
    run = await fetch_run(_registry_factory(), run_id)
    # 404 for both missing and non-owned runs so a caller cannot probe run_ids they do not own
    if not run or run["user_id"] != user.user_id:
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
            status=run["status"],
            error=run["error"],
        )


@router.post("/api/runs/{run_id}/approve", response_model=RunStatusResponse)
async def approve_picks(run_id: str, req: ApprovePicksRequest, user: Principal = Depends(require_user)):
    registry = _registry_factory()
    run = await fetch_run(registry, run_id)
    if not run or run["user_id"] != user.user_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"Run is {run['status']}, not awaiting_review")

    graph = get_graph()
    config = {
        "configurable": {"thread_id": run_id},
        "run_name": f"approve/{run_id[:8]}",
        "metadata": {
            "run_id": run_id,
            "user_id": user.user_id,
            "approved_count": len(req.approved_pick_ids),
        },
    }

    from langgraph.types import Command

    try:
        result = await graph.ainvoke(
            Command(resume=req.approved_pick_ids),
            config=config,
        )
        await update_run(registry, run_id, "complete")
        approved = [p.model_dump() if hasattr(p, "model_dump") else p for p in (result.get("approved_picks") or [])]
        return RunStatusResponse(run_id=run_id, status="complete", approved_picks=approved)
    except Exception:
        logger.exception("approve_picks: resume failed for run_id=%s", run_id)
        await update_run(registry, run_id, "error", error="Approval failed. See server logs.")
        raise HTTPException(status_code=500, detail="Approval failed")


@router.get("/api/picks", response_model=list[PickRecord])
async def list_picks(user: Principal = Depends(require_user), limit: int = Query(50, ge=1, le=200)):
    """Return the caller's approved picks, ordered by approval time, newest first.

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
            .where(Pick.user_id == user.user_id)
            .order_by(desc(Pick.approved_at))
            .limit(limit)
        )
        result = await session.execute(q)
        picks = result.scalars().all()

    return [PickRecord.model_validate(p) for p in picks]


class SteamCandidateRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    sport: str
    game_id: str
    home_team: str
    away_team: str
    commence_time: datetime | None = None
    market: str
    selection: str
    book: str
    price: int
    sharp_probability: float
    implied_probability: float
    edge_pct: float
    ev_pct: float
    rationale: str
    status: str


@router.get("/api/steam", response_model=list[SteamCandidateRecord])
async def list_steam_candidates(user: Principal = Depends(require_user)):
    """Pending steam candidates awaiting review, newest first."""
    from fairline.db.models import SteamCandidate

    try:
        factory = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(SteamCandidate)
                    .where(SteamCandidate.status == "pending")
                    .order_by(desc(SteamCandidate.created_at))
                )
            )
            .scalars()
            .all()
        )
    return [SteamCandidateRecord.model_validate(c) for c in rows]


@router.post("/api/steam/{candidate_id}/approve")
async def approve_steam(candidate_id: str, user: Principal = Depends(require_user)):
    from fairline.steam import approve_steam_candidate

    try:
        factory = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    pick_id = await approve_steam_candidate(factory, candidate_id, user.user_id)
    if pick_id is None:
        raise HTTPException(status_code=404, detail="Candidate not found or already resolved")
    return {"pick_id": pick_id, "status": "approved"}


@router.post("/api/steam/{candidate_id}/reject")
async def reject_steam(candidate_id: str, user: Principal = Depends(require_user)):
    from fairline.steam import reject_steam_candidate

    try:
        factory = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if not await reject_steam_candidate(factory, candidate_id):
        raise HTTPException(status_code=404, detail="Candidate not found or already resolved")
    return {"status": "rejected"}


@router.get("/api/trends")
async def team_trends(
    team: str,
    last_n: int = Query(10, ge=1, le=50),
    user: Principal = Depends(require_user),
):
    """SU, ATS, and O/U records for one team from stored game results."""
    from sqlalchemy import or_

    from fairline.db.models import GameResult
    from fairline.trends import compute_team_trends

    try:
        factory = get_session_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    async with factory() as session:
        results = (
            (
                await session.execute(
                    select(GameResult).where(
                        or_(GameResult.home_team == team, GameResult.away_team == team)
                    )
                )
            )
            .scalars()
            .all()
        )
    if not results:
        raise HTTPException(status_code=404, detail=f"no recorded games for {team!r}")
    return {"team": team, **compute_team_trends(results, team, last_n=last_n)}


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
