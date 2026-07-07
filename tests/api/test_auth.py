"""API key authentication: header parsing, hash lookup, and demo fallback."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from steambot.api.auth import authenticate, issue_api_key
from steambot.db.models import Base


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_valid_key_returns_principal(session_factory):
    user_id, key = await issue_api_key("alice@example.com", session_factory)
    assert key.startswith("sb_")

    principal = await authenticate(f"Bearer {key}", session_factory)
    assert principal.user_id == user_id
    assert principal.is_pro is False


async def test_missing_header_is_401(session_factory):
    with pytest.raises(HTTPException) as exc:
        await authenticate(None, session_factory)
    assert exc.value.status_code == 401


async def test_malformed_header_is_401(session_factory):
    with pytest.raises(HTTPException) as exc:
        await authenticate("Basic dXNlcjpwYXNz", session_factory)
    assert exc.value.status_code == 401


async def test_unknown_key_is_401(session_factory):
    with pytest.raises(HTTPException) as exc:
        await authenticate("Bearer sb_not-a-real-key", session_factory)
    assert exc.value.status_code == 401


async def test_no_database_falls_back_to_demo_principal():
    principal = await authenticate(None, None)
    assert principal.user_id == "demo"


async def test_reissue_rotates_key(session_factory):
    user_id, old_key = await issue_api_key("alice@example.com", session_factory)
    same_id, new_key = await issue_api_key("alice@example.com", session_factory)

    assert same_id == user_id
    assert new_key != old_key
    with pytest.raises(HTTPException):
        await authenticate(f"Bearer {old_key}", session_factory)
    principal = await authenticate(f"Bearer {new_key}", session_factory)
    assert principal.user_id == user_id
