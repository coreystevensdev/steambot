"""Server-rendered pages: leaderboard, review queue, record."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import fairline.db.session as db_session


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FAIRLINE_ENV", raising=False)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_session_factory", None)
    from fairline.api.main import app

    with TestClient(app) as c:
        yield c


def test_leaderboard_is_the_front_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Agent record" in resp.text
    assert "No settled picks yet" in resp.text


def test_queue_page_renders_empty_state(client):
    resp = client.get("/queue")
    assert resp.status_code == 200
    assert "Review queue" in resp.text
    assert "Nothing pending" in resp.text


def test_record_page_renders_empty_state(client):
    resp = client.get("/record")
    assert resp.status_code == 200
    assert "The record" in resp.text


def test_tokens_stylesheet_is_served(client):
    resp = client.get("/static/tokens.css")
    assert resp.status_code == 200
    assert "--accent" in resp.text
