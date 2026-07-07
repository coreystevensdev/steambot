"""Tests for line-history capture: window filtering, snapshot flattening, storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, LineSnapshot
from fairline.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome
from fairline.steam import games_in_window, record_snapshots, snapshot_rows

NOW = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _game(game_id: str = "game-1", kickoff: datetime | None = None) -> GameSnapshot:
    return GameSnapshot(
        game_id=game_id,
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=kickoff or (NOW + timedelta(hours=2)),
        bookmakers=[
            BookmakerOdds(
                key="pinnacle",
                title="Pinnacle",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=-118, point=-3.5),
                            Outcome(name="Las Vegas Raiders", price=-102, point=3.5),
                        ],
                    )
                ],
            ),
            BookmakerOdds(
                key="draftkings",
                title="DraftKings",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[
                            Outcome(name="Kansas City Chiefs", price=-110, point=-3.5),
                            Outcome(name="Las Vegas Raiders", price=-110, point=3.5),
                        ],
                    )
                ],
            ),
            BookmakerOdds(
                key="unibet",  # not in the tracked book set
                title="Unibet",
                markets=[
                    MarketOdds(
                        key="spreads",
                        outcomes=[Outcome(name="Kansas City Chiefs", price=-109, point=-3.5)],
                    )
                ],
            ),
        ],
    )


def test_window_keeps_upcoming_games_only():
    soon = _game("g-soon", NOW + timedelta(hours=2))
    started = _game("g-started", NOW - timedelta(minutes=5))
    far = _game("g-far", NOW + timedelta(hours=30))

    kept = games_in_window([soon, started, far], now=NOW, window_hours=3)

    assert [g.game_id for g in kept] == ["g-soon"]


def test_snapshot_rows_flattens_tracked_books_only():
    rows = snapshot_rows([_game()], captured_at=NOW)

    books = {r.book for r in rows}
    assert books == {"pinnacle", "draftkings"}
    assert len(rows) == 4  # 2 books x 2 outcomes
    pin = next(r for r in rows if r.book == "pinnacle" and r.outcome == "Kansas City Chiefs")
    assert pin.price == -118
    assert pin.point == -3.5
    assert pin.market == "spreads"
    assert pin.captured_at == NOW


async def test_record_snapshots_persists_rows(session_factory):
    written = await record_snapshots([_game()], session_factory, captured_at=NOW)

    assert written == 4
    async with session_factory() as session:
        rows = (await session.execute(select(LineSnapshot))).scalars().all()
    assert len(rows) == 4
    assert {r.game_id for r in rows} == {"game-1"}


def _rows(captured_at, price_a=-110, price_b=-110, point_a=-2.5, point_b=2.5, market="spreads", sport="americanfootball_nfl"):
    common = dict(game_id="game-1", sport=sport, book="pinnacle", market=market, captured_at=captured_at)
    return [
        LineSnapshot(outcome="Kansas City Chiefs", price=price_a, point=point_a, **common),
        LineSnapshot(outcome="Las Vegas Raiders", price=price_b, point=point_b, **common),
    ]


class TestCrossedKeyNumber:
    def test_crossing_three(self):
        from fairline.steam import crossed_key_number

        assert crossed_key_number(-2.5, -3.5) is True

    def test_landing_on_seven(self):
        from fairline.steam import crossed_key_number

        assert crossed_key_number(-6.5, -7.0) is True

    def test_move_between_keys(self):
        from fairline.steam import crossed_key_number

        assert crossed_key_number(-7.5, -8.5) is False

    def test_no_move(self):
        from fairline.steam import crossed_key_number

        assert crossed_key_number(-2.5, -2.5) is False


class TestDetectSteam:
    def test_prob_move_over_threshold_fires(self):
        from fairline.steam import detect_steam

        old = _rows(NOW, price_a=-110, price_b=-110, market="h2h", point_a=None, point_b=None)
        new = _rows(NOW + timedelta(minutes=6), price_a=-125, price_b=105, market="h2h", point_a=None, point_b=None)

        events = detect_steam(old, new)

        assert len(events) == 1
        e = events[0]
        assert e.outcome == "Kansas City Chiefs"
        # -125/+105 devigs to .5325 for the favorite, from .5000
        assert e.prob_move == pytest.approx(0.0325, abs=0.001)
        assert e.crossed_key is False

    def test_small_move_is_silent(self):
        from fairline.steam import detect_steam

        old = _rows(NOW, price_a=-110, price_b=-110)
        new = _rows(NOW + timedelta(minutes=6), price_a=-112, price_b=-108)

        assert detect_steam(old, new) == []

    def test_stale_baseline_is_ignored(self):
        from fairline.steam import detect_steam

        old = _rows(NOW, price_a=-110, price_b=-110, market="h2h", point_a=None, point_b=None)
        new = _rows(NOW + timedelta(minutes=45), price_a=-130, price_b=110, market="h2h", point_a=None, point_b=None)

        assert detect_steam(old, new) == []

    def test_key_number_crossing_fires_without_price_move(self):
        from fairline.steam import detect_steam

        old = _rows(NOW, point_a=-2.5, point_b=2.5)
        new = _rows(NOW + timedelta(minutes=6), point_a=-3.0, point_b=3.0)

        events = detect_steam(old, new)

        assert len(events) == 1
        e = events[0]
        assert e.outcome == "Kansas City Chiefs"
        assert e.crossed_key is True
        assert e.old_point == -2.5
        assert e.new_point == -3.0


async def test_scan_recent_steam_compares_latest_against_baseline(session_factory):
    from fairline.steam import scan_recent_steam

    async with session_factory() as session:
        session.add_all(_rows(NOW - timedelta(minutes=8), price_a=-110, price_b=-110, market="h2h", point_a=None, point_b=None))
        session.add_all(_rows(NOW, price_a=-125, price_b=105, market="h2h", point_a=None, point_b=None))
        await session.commit()

    events = await scan_recent_steam(session_factory, lookback_minutes=12)

    assert len(events) == 1
    assert events[0].outcome == "Kansas City Chiefs"


async def test_scan_with_single_cycle_returns_nothing(session_factory):
    from fairline.steam import scan_recent_steam

    async with session_factory() as session:
        session.add_all(_rows(NOW))
        await session.commit()

    assert await scan_recent_steam(session_factory, lookback_minutes=12) == []


def test_key_numbers_do_not_apply_outside_nfl():
    from fairline.steam import detect_steam

    old = _rows(NOW, point_a=-2.5, point_b=2.5)
    new = _rows(NOW + timedelta(minutes=6), point_a=-3.0, point_b=3.0)
    for rows in (old, new):
        for r in rows:
            r.sport = "basketball_nba"

    assert detect_steam(old, new) == []


def _upcoming_game():
    return GameSnapshot(
        game_id="game-1",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=NOW + timedelta(hours=1),
        bookmakers=[],
    )


def _retail_row(book="draftkings", price=-108, point=-2.5, captured_at=None):
    return LineSnapshot(
        game_id="game-1",
        sport="americanfootball_nfl",
        book=book,
        market="spreads",
        outcome="Kansas City Chiefs",
        price=price,
        point=point,
        captured_at=captured_at or NOW,
    )


async def _steam_event(session_factory):
    from fairline.steam import scan_recent_steam

    async with session_factory() as session:
        session.add_all(_rows(NOW - timedelta(minutes=8), price_a=-110, price_b=-110))
        session.add_all(_rows(NOW, price_a=-135, price_b=115))
        await session.commit()
    events = await scan_recent_steam(session_factory)
    assert len(events) == 1
    return events[0]


async def test_create_candidates_flags_lagging_retail_books(session_factory):
    from fairline.db.models import SteamCandidate
    from fairline.steam import create_steam_candidates

    event = await _steam_event(session_factory)
    assert event.new_prob > 0.53  # -135/+115 devigs to ~.545 for the steamed side

    async with session_factory() as session:
        session.add(_retail_row("draftkings", price=-105))  # stale: implied .512 vs fair ~.545
        session.add(_retail_row("fanduel", price=-125))  # already moved: implied .556
        await session.commit()

    created = await create_steam_candidates(session_factory, [event], [_upcoming_game()])

    assert created == 1
    async with session_factory() as session:
        cand = (await session.execute(select(SteamCandidate))).scalars().one()
    assert cand.book == "draftkings"
    assert cand.selection == "Kansas City Chiefs -2.5"
    assert cand.status == "pending"
    assert cand.edge_pct > 0.02


async def test_create_candidates_does_not_duplicate_pending(session_factory):
    from fairline.db.models import SteamCandidate
    from fairline.steam import create_steam_candidates

    event = await _steam_event(session_factory)
    async with session_factory() as session:
        session.add(_retail_row("draftkings", price=-105))
        await session.commit()

    await create_steam_candidates(session_factory, [event], [_upcoming_game()])
    await create_steam_candidates(session_factory, [event], [_upcoming_game()])

    async with session_factory() as session:
        rows = (await session.execute(select(SteamCandidate))).scalars().all()
    assert len(rows) == 1


async def test_approve_candidate_creates_steam_pick(session_factory):
    from fairline.db.models import Pick, SteamCandidate
    from fairline.steam import approve_steam_candidate, create_steam_candidates

    event = await _steam_event(session_factory)
    async with session_factory() as session:
        session.add(_retail_row("draftkings", price=-105))
        await session.commit()
    await create_steam_candidates(session_factory, [event], [_upcoming_game()])

    async with session_factory() as session:
        cand_id = (await session.execute(select(SteamCandidate))).scalars().one().id

    pick_id = await approve_steam_candidate(session_factory, cand_id, "alice")

    assert pick_id is not None
    async with session_factory() as session:
        pick = (await session.execute(select(Pick))).scalars().one()
        cand = (await session.execute(select(SteamCandidate))).scalars().one()
    assert pick.source == "steam"
    assert pick.user_id == "alice"
    assert pick.book == "draftkings"
    assert cand.status == "approved"

    # a second approval attempt is a no-op
    assert await approve_steam_candidate(session_factory, cand_id, "bob") is None
