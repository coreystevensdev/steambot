"""Ownership checks on the run endpoints.

Identity comes from the authenticated principal, never from request fields. A
caller who knows a run_id but does not own the run must not be able to read its
candidates or resume it. The rejection has to fire before any graph interaction,
so these call the endpoint functions directly against the in-memory registry
(no DATABASE_URL) with no live graph.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import fairline.api.main  # noqa: F401 -- load main first; it defines get_graph before importing routes
import fairline.db.session as db_session
from fairline import runs
from fairline.api import routes
from fairline.api.auth import Principal

ALICE = Principal(user_id="alice", is_pro=False)
MALLORY = Principal(user_id="mallory", is_pro=False)


@pytest.fixture(autouse=True)
def memory_registry(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_session_factory", None)
    runs._memory_runs.clear()
    yield
    runs._memory_runs.clear()


async def test_get_run_rejects_non_owner():
    await runs.create_run(None, "r1", "alice")
    with pytest.raises(HTTPException) as exc:
        await routes.get_run("r1", user=MALLORY)
    assert exc.value.status_code == 404


async def test_approve_picks_rejects_non_owner():
    await runs.create_run(None, "r1", "alice")
    await runs.update_run(None, "r1", "awaiting_review")
    req = routes.ApprovePicksRequest(approved_pick_ids=["p1"])
    with pytest.raises(HTTPException) as exc:
        await routes.approve_picks("r1", req, user=MALLORY)
    assert exc.value.status_code == 404


async def test_unknown_run_is_404_not_leaked():
    with pytest.raises(HTTPException) as exc:
        await routes.get_run("does-not-exist", user=ALICE)
    assert exc.value.status_code == 404
