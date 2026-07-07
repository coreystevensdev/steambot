"""Tests for the matchup engine: player stats, splits, shrinkage, prop grading."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fairline.db.models import Base, PlayerGame
from fairline.matchup import (
    combine_splits,
    compute_prop_splits,
    matchup_probability,
    parse_player_stats,
    shrunk_probability,
)

NOW = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _pg(week: int, passing: float, opponent: str = "Las Vegas Raiders", season: int = 2025) -> PlayerGame:
    return PlayerGame(
        sport="americanfootball_nfl",
        season=season,
        week=week,
        game_date=NOW - timedelta(days=7 * (20 - week)),
        player="Patrick Mahomes",
        team="Kansas City Chiefs",
        opponent=opponent,
        passing_yards=passing,
    )


def test_shrinkage_pulls_toward_base_rate():
    # 8 of 10 at a 50% base with k=10: (8 + 5) / 20 = 0.65
    assert shrunk_probability(8, 10, 0.5, k=10) == pytest.approx(0.65)
    # tiny sample barely moves off base
    assert shrunk_probability(2, 2, 0.5, k=10) == pytest.approx(7 / 12)


def test_splits_are_preregistered_and_counted():
    games = [_pg(w, 300.0 if w % 2 else 250.0) for w in range(1, 11)]

    splits = compute_prop_splits(games, "passing_yards", line=275.5)

    assert set(splits) == {"last_5", "last_10", "season", "vs_opponent"}
    assert splits["last_10"] == (5, 10)
    assert splits["season"] == (5, 10)
    # weeks 6..10: odd weeks 7 and 9 clear the line
    assert splits["last_5"] == (2, 5)


def test_combine_splits_is_bounded_near_the_market():
    # perfect 10-for-10 cannot drag the final number far from the fair prob
    splits = {"last_10": (10, 10), "season": (10, 10)}
    p = combine_splits(splits, base_rate=0.5, market_fair=0.5)
    assert p <= 0.5 + 0.06 + 1e-9
    assert p > 0.5


def test_matchup_probability_under_side_complements():
    games = [_pg(w, 300.0) for w in range(1, 11)]
    over, _ = matchup_probability(games, "passing_yards", 275.5, "Over", market_fair=0.5)
    under, _ = matchup_probability(games, "passing_yards", 275.5, "Under", market_fair=0.5)
    assert over > 0.5 > under
    assert over + under == pytest.approx(1.0)


def test_parse_player_stats_maps_teams_and_filters_empty_rows():
    csv_text = (
        "player_display_name,position,recent_team,opponent_team,season,week,"
        "passing_yards,rushing_yards,receiving_yards,receptions\n"
        "Patrick Mahomes,QB,KC,LV,2025,10,291,12,,\n"
        "Some Lineman,G,KC,LV,2025,10,,,,\n"
    )
    rows = parse_player_stats(csv_text, {("KC", 2025, 10): NOW})

    assert len(rows) == 1
    r = rows[0]
    assert r.player == "Patrick Mahomes"
    assert r.team == "Kansas City Chiefs"
    assert r.opponent == "Las Vegas Raiders"
    assert r.passing_yards == 291.0
    assert r.game_date == NOW


async def test_grade_prop_picks_from_box_scores(session_factory):
    from fairline.db.models import Pick
    from fairline.matchup import grade_prop_picks

    async with session_factory() as session:
        session.add(_pg(10, 291.0))
        session.add(
            Pick(
                id="prop-1",
                user_id="u1",
                run_id="matchup:1",
                sport="americanfootball_nfl",
                game_id="evt-1",
                home_team="Kansas City Chiefs",
                away_team="Las Vegas Raiders",
                commence_time=NOW - timedelta(days=7 * 10),
                market="player_pass_yds",
                selection="Patrick Mahomes Over 275.5",
                book="draftkings",
                price=105,
                sharp_probability=0.51,
                blended_probability=0.51,
                edge_pct=0.03,
                ev_pct=0.04,
                confidence="medium",
                rationale="test",
                source="matchup",
                approved_at=NOW - timedelta(days=71),
            )
        )
        await session.commit()

    # the player game sits at week 10's date; align the pick to it
    async with session_factory() as session:
        pick = (await session.execute(select(Pick))).scalars().one()
        pg = (await session.execute(select(PlayerGame))).scalars().one()
        pick.commence_time = pg.game_date
        await session.commit()

    summary = await grade_prop_picks(session_factory)

    assert summary == {"graded": 1, "missed": 0}
    async with session_factory() as session:
        pick = (await session.execute(select(Pick))).scalars().one()
    assert pick.result == "win"
    assert pick.profit_units == pytest.approx(1.05)


async def test_grade_prop_picks_push_and_missing_player(session_factory):
    from fairline.db.models import Pick
    from fairline.matchup import grade_prop_picks

    def _prop_pick(pid: str, selection: str) -> Pick:
        return Pick(
            id=pid,
            user_id="u1",
            run_id="matchup:1",
            sport="americanfootball_nfl",
            game_id="evt-1",
            home_team="Kansas City Chiefs",
            away_team="Las Vegas Raiders",
            commence_time=NOW,
            market="player_pass_yds",
            selection=selection,
            book="draftkings",
            price=-110,
            sharp_probability=0.5,
            blended_probability=0.5,
            edge_pct=0.02,
            ev_pct=0.02,
            confidence="low",
            rationale="test",
            source="matchup",
            approved_at=NOW,
        )

    async with session_factory() as session:
        pg = _pg(10, 275.0)
        pg.game_date = NOW
        session.add(pg)
        session.add(_prop_pick("p-push", "Patrick Mahomes Over 275"))
        session.add(_prop_pick("p-miss", "Unknown Player Over 100.5"))
        await session.commit()

    summary = await grade_prop_picks(session_factory)

    assert summary == {"graded": 1, "missed": 1}
    async with session_factory() as session:
        graded = (await session.execute(select(Pick).where(Pick.id == "p-push"))).scalars().one()
    assert graded.result == "push"
    assert graded.profit_units == 0.0


async def test_create_matchup_candidates_queues_and_approves(session_factory):
    from fairline.db.models import Pick, SteamCandidate
    from fairline.matchup import create_matchup_candidates
    from fairline.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome
    from fairline.steam import approve_steam_candidate

    async with session_factory() as session:
        # ten straight 300-yard games: history strongly supports the Over
        for w in range(1, 11):
            session.add(_pg(w, 300.0))
        await session.commit()

    def _o(side, price, book_point=275.5):
        return Outcome(name=side, price=price, point=book_point, description="Patrick Mahomes")

    snapshot = GameSnapshot(
        game_id="evt-1",
        sport="americanfootball_nfl",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=NOW,
        bookmakers=[
            BookmakerOdds(key="pinnacle", title="P", markets=[
                MarketOdds(key="player_pass_yds", outcomes=[_o("Over", -110), _o("Under", -110)])
            ]),
            BookmakerOdds(key="draftkings", title="DK", markets=[
                MarketOdds(key="player_pass_yds", outcomes=[_o("Over", 100), _o("Under", -120)])
            ]),
        ],
    )

    created = await create_matchup_candidates(session_factory, snapshot, min_edge=0.03)

    assert created == 1
    async with session_factory() as session:
        cand = (await session.execute(select(SteamCandidate))).scalars().one()
    assert cand.source == "matchup"
    assert cand.selection == "Patrick Mahomes Over 275.5"
    assert "last_10 10-0" in cand.rationale

    # re-run is a no-op; approval carries the matchup source onto the pick
    assert await create_matchup_candidates(session_factory, snapshot, min_edge=0.03) == 0
    pick_id = await approve_steam_candidate(session_factory, cand.id, "alice")
    assert pick_id is not None
    async with session_factory() as session:
        pick = (await session.execute(select(Pick).where(Pick.id == pick_id))).scalars().one()
    assert pick.source == "matchup"
