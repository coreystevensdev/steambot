"""API key authentication.

Keys are sb_-prefixed random tokens shown once at issue time; only the SHA-256
hash is stored. Identity always comes from the key, never from request fields,
so ownership checks downstream compare against a server-derived principal.

With no database configured (local demo), every request runs as the "demo"
principal with no credentials. STEAMBOT_ENV=production refuses to boot without
a database, so production always enforces keys.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import NamedTuple

from fastapi import Header, HTTPException
from sqlalchemy import select

from steambot.db.models import User


class Principal(NamedTuple):
    user_id: str
    is_pro: bool


DEMO_PRINCIPAL = Principal(user_id="demo", is_pro=False)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def authenticate(authorization: str | None, session_factory) -> Principal:
    if session_factory is None:
        return DEMO_PRINCIPAL

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    key = authorization.removeprefix("Bearer ").strip()

    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.api_key_hash == _hash_key(key))
        )
        user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return Principal(user_id=user.id, is_pro=user.is_pro)


async def require_user(authorization: str | None = Header(None)) -> Principal:
    """FastAPI dependency: resolve the caller from the Authorization header."""
    from steambot.db.session import get_session_factory

    try:
        factory = get_session_factory()
    except RuntimeError:
        factory = None
    return await authenticate(authorization, factory)


async def issue_api_key(email: str, session_factory) -> tuple[str, str]:
    """Create a user (or rotate an existing user's key) and return (user_id, key).

    The plaintext key exists only in the return value; callers must show it
    once and discard it.
    """
    key = f"sb_{secrets.token_urlsafe(32)}"
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(id=str(uuid.uuid4()), email=email, api_key_hash=_hash_key(key))
            session.add(user)
        else:
            user.api_key_hash = _hash_key(key)
        await session.commit()
        return user.id, key
