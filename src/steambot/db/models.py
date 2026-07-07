"""SQLAlchemy ORM models for pick history and CLV tracking."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # SHA-256 of the sb_ API key; the plaintext is shown once at issue and never stored
    api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255))
    is_pro: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Pick(Base):
    """Approved picks recorded for CLV and ROI tracking."""

    __tablename__ = "picks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    game_id: Mapped[str] = mapped_column(String(255), nullable=False)
    home_team: Mapped[str] = mapped_column(String(100))
    away_team: Mapped[str] = mapped_column(String(100))
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market: Mapped[str] = mapped_column(String(50))
    selection: Mapped[str] = mapped_column(String(255))
    book: Mapped[str] = mapped_column(String(100))
    price: Mapped[int] = mapped_column(Integer)
    sharp_probability: Mapped[float] = mapped_column(Float)
    blended_probability: Mapped[float] = mapped_column(Float)
    edge_pct: Mapped[float] = mapped_column(Float)
    ev_pct: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(20))
    rationale: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Outcome fields -- filled by `steambot settle` (closing line) and `steambot grade` (result)
    closing_price: Mapped[int | None] = mapped_column(Integer)
    # closing spread/total line; NULL for h2h. Makes point drift (-3.5 -> -4.0) queryable
    closing_point: Mapped[float | None] = mapped_column(Float)
    closing_probability: Mapped[float | None] = mapped_column(Float)
    # CLV = closing_probability - implied prob of the taken price (positive = beat the close)
    clv: Mapped[float | None] = mapped_column(Float)
    result: Mapped[str | None] = mapped_column(String(10))  # "win" | "loss" | "push"
    profit_units: Mapped[float | None] = mapped_column(Float)
