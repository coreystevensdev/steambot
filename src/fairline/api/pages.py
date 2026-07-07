"""Server-rendered pages: leaderboard, review queue, the record.

Read pages are public on purpose: the graded record is the product's claim,
and a claim behind a login is marketing. Mutations (approve/reject) go to the
existing authed API endpoints; the page attaches the operator's key from
localStorage via htmx.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from fairline.db.session import get_session_factory

_WEB = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(_WEB / "templates"))
router = APIRouter()


def _factory():
    try:
        return get_session_factory()
    except RuntimeError:
        return None


@router.get("/", include_in_schema=False)
async def agents_page(request: Request):
    from fairline.clv import agent_report

    factory = _factory()
    report = await agent_report(factory) if factory else {}
    return templates.TemplateResponse(
        request, "agents.html", {"page": "agents", "report": report}
    )


@router.get("/queue", include_in_schema=False)
async def queue_page(request: Request):
    from fairline.db.models import SteamCandidate

    factory = _factory()
    candidates = []
    if factory:
        async with factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(SteamCandidate)
                        .where(SteamCandidate.status == "pending")
                        .order_by(desc(SteamCandidate.created_at))
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
    return templates.TemplateResponse(
        request, "queue.html", {"page": "queue", "candidates": candidates}
    )


@router.get("/record", include_in_schema=False)
async def record_page(request: Request):
    from fairline.db.models import Pick

    factory = _factory()
    picks = []
    if factory:
        async with factory() as session:
            picks = (
                (
                    await session.execute(
                        select(Pick)
                        .where(Pick.result.is_not(None))
                        .order_by(desc(Pick.approved_at))
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
    settled_clv = [p.clv for p in picks if p.clv is not None]
    totals = {
        "count": len(picks),
        "avg_clv": sum(settled_clv) / len(settled_clv) if settled_clv else 0.0,
        "units": sum(p.profit_units or 0.0 for p in picks),
    }
    return templates.TemplateResponse(
        request, "record.html", {"page": "record", "picks": picks, "totals": totals}
    )
