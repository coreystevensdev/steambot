"""SQLAlchemy ORM models for pick history and CLV tracking."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # SHA-256 of the fl_ API key; the plaintext is shown once at issue and never stored
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
    sport: Mapped[str] = mapped_column(
        String(50), nullable=False, default="americanfootball_nfl", server_default="americanfootball_nfl"
    )
    game_id: Mapped[str] = mapped_column(String(255), nullable=False)
    home_team: Mapped[str] = mapped_column(String(100))
    away_team: Mapped[str] = mapped_column(String(100))
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market: Mapped[str] = mapped_column(String(50))
    selection: Mapped[str] = mapped_column(String(255))
    book: Mapped[str] = mapped_column(String(100))
    price: Mapped[int] = mapped_column(Integer)
    sharp_probability: Mapped[float] = mapped_column(Float)
    # caller-supplied simulation estimate; NULL when no sim was provided for this pick
    sim_probability: Mapped[float | None] = mapped_column(Float)
    blended_probability: Mapped[float] = mapped_column(Float)
    edge_pct: Mapped[float] = mapped_column(Float)
    ev_pct: Mapped[float] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(20))
    rationale: Mapped[str] = mapped_column(Text)
    # producing agent ("model", "steam", ...); the agent leaderboard groups on this
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="model", server_default="model")
    # comma-separated split names that fed a matchup pick; NULL for other agents
    angles: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Outcome fields -- filled by `fairline settle` (closing line) and `fairline grade` (result)
    closing_price: Mapped[int | None] = mapped_column(Integer)
    # closing spread/total line; NULL for h2h. Makes point drift (-3.5 -> -4.0) queryable
    closing_point: Mapped[float | None] = mapped_column(Float)
    closing_probability: Mapped[float | None] = mapped_column(Float)
    # CLV = closing_probability - implied prob of the taken price (positive = beat the close)
    clv: Mapped[float | None] = mapped_column(Float)
    result: Mapped[str | None] = mapped_column(String(10))  # "win" | "loss" | "push"
    profit_units: Mapped[float | None] = mapped_column(Float)


class LineSnapshot(Base):
    """One book's price for one side of a market at one moment.

    The raw material for steam detection: comparing a book's rows across
    captured_at reveals line movement the single-shot odds fetch cannot see.
    """

    __tablename__ = "line_snapshots"
    __table_args__ = (
        Index("ix_line_snapshots_lookup", "game_id", "market", "book", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sport: Mapped[str] = mapped_column(
        String(50), nullable=False, default="americanfootball_nfl", server_default="americanfootball_nfl"
    )
    book: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    point: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GameResult(Base):
    """One completed game with its closing line, for trend records.

    Covers every game seen by the watcher and scores feed, not just picked
    ones; ATS and O/U records need the full schedule.
    """

    __tablename__ = "game_results"

    game_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    sport: Mapped[str] = mapped_column(String(50), nullable=False)
    home_team: Mapped[str] = mapped_column(String(100), nullable=False)
    away_team: Mapped[str] = mapped_column(String(100), nullable=False)
    commence_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    home_score: Mapped[int] = mapped_column(Integer, nullable=False)
    away_score: Mapped[int] = mapped_column(Integer, nullable=False)
    # home team's closing handicap (-3.5 = home favored); NULL when no snapshot exists
    closing_spread_home: Mapped[float | None] = mapped_column(Float)
    closing_total: Mapped[float | None] = mapped_column(Float)


class Run(Base):
    """Run registry row: status and ownership only.

    Graph state (candidates, approvals) lives in the LangGraph checkpointer;
    this table exists so ownership checks and status survive restarts and are
    shared across processes (the API and the steam watcher).
    """

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SteamCandidate(Base):
    """A deterministic pick candidate awaiting human approval.

    Named for its first producer, now the shared review queue: steam and
    matchup candidates both land here (distinguished by source), and approval
    turns one into a Pick carrying that source into settlement and grading.
    """

    __tablename__ = "steam_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="steam", server_default="steam")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sport: Mapped[str] = mapped_column(String(50), nullable=False)
    game_id: Mapped[str] = mapped_column(String(255), nullable=False)
    home_team: Mapped[str] = mapped_column(String(100), nullable=False)
    away_team: Mapped[str] = mapped_column(String(100), nullable=False)
    commence_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    market: Mapped[str] = mapped_column(String(50), nullable=False)
    selection: Mapped[str] = mapped_column(String(255), nullable=False)
    book: Mapped[str] = mapped_column(String(100), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    sharp_probability: Mapped[float] = mapped_column(Float, nullable=False)
    implied_probability: Mapped[float] = mapped_column(Float, nullable=False)
    edge_pct: Mapped[float] = mapped_column(Float, nullable=False)
    ev_pct: Mapped[float] = mapped_column(Float, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    angles: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending", server_default="pending")


class PlayerGame(Base):
    """One player's stat line for one game, the matchup agent's raw material."""

    __tablename__ = "player_games"
    __table_args__ = (Index("ix_player_games_player", "sport", "player", "season", "week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sport: Mapped[str] = mapped_column(
        String(50), nullable=False, default="americanfootball_nfl", server_default="americanfootball_nfl"
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    game_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    player: Mapped[str] = mapped_column(String(100), nullable=False)
    team: Mapped[str] = mapped_column(String(100), nullable=False)
    opponent: Mapped[str] = mapped_column(String(100), nullable=False)
    passing_yards: Mapped[float | None] = mapped_column(Float)
    rushing_yards: Mapped[float | None] = mapped_column(Float)
    receiving_yards: Mapped[float | None] = mapped_column(Float)
    receptions: Mapped[float | None] = mapped_column(Float)


class MlbPlayerGame(Base):
    """One MLB batter's stat line for one game, with the game context the
    situational splits need (day/night, home/away, opposing starter)."""

    __tablename__ = "mlb_player_games"
    __table_args__ = (Index("ix_mlb_player_games_player", "player", "season"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    game_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    player: Mapped[str] = mapped_column(String(100), nullable=False)
    team: Mapped[str] = mapped_column(String(100), nullable=False)
    opponent: Mapped[str] = mapped_column(String(100), nullable=False)
    # the starting pitcher this batter faced; NULL when not yet derived/backfilled
    opposing_pitcher: Mapped[str | None] = mapped_column(String(100))
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False)
    day_night: Mapped[str] = mapped_column(String(10), nullable=False)  # "day" | "night"
    at_bats: Mapped[int | None] = mapped_column(Integer)
    hits: Mapped[int | None] = mapped_column(Integer)
    home_runs: Mapped[int | None] = mapped_column(Integer)
    rbis: Mapped[int | None] = mapped_column(Integer)
    total_bases: Mapped[int | None] = mapped_column(Integer)
    strikeouts: Mapped[int | None] = mapped_column(Integer)
    walks: Mapped[int | None] = mapped_column(Integer)
