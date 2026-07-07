"""Ownership checks on the run endpoints.

Identity comes from the authenticated principal, never from request fields. A
caller who knows a run_id but does not own the run must not be able to read its
candidates or resume it. The rejection has to fire before any graph interaction,
so these call the endpoint functions directly with a seeded registry and no live
graph.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import steambot.api.main  # noqa: F401 -- load main first; it defines get_graph before importing routes
from steambot.api import routes
from steambot.api.auth import Principal

ALICE = Principal(user_id="alice", is_pro=False)
MALLORY = Principal(user_id="mallory", is_pro=False)


@pytest.fixture(autouse=True)
def clear_runs():
    routes._runs.clear()
    yield
    routes._runs.clear()


async def test_get_run_rejects_non_owner():
    routes._runs["r1"] = {"status": "awaiting_review", "state": {}, "owner": "alice"}
    with pytest.raises(HTTPException) as exc:
        await routes.get_run("r1", user=MALLORY)
    assert exc.value.status_code == 404


async def test_approve_picks_rejects_non_owner():
    routes._runs["r1"] = {"status": "awaiting_review", "state": {}, "owner": "alice"}
    req = routes.ApprovePicksRequest(approved_pick_ids=["p1"])
    with pytest.raises(HTTPException) as exc:
        await routes.approve_picks("r1", req, user=MALLORY)
    assert exc.value.status_code == 404


async def test_unknown_run_is_404_not_leaked():
    with pytest.raises(HTTPException) as exc:
        await routes.get_run("does-not-exist", user=ALICE)
    assert exc.value.status_code == 404
