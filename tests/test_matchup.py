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


async def test_candidates_record_their_angles_and_report_grades_them(session_factory):
    from fairline.db.models import Pick, SteamCandidate
    from fairline.matchup import angle_report, create_matchup_candidates
    from fairline.state import BookmakerOdds, GameSnapshot, MarketOdds, Outcome
    from fairline.steam import approve_steam_candidate

    async with session_factory() as session:
        for w in range(1, 11):
            session.add(_pg(w, 300.0))
        await session.commit()

    def _o(side, price):
        return Outcome(name=side, price=price, point=275.5, description="Patrick Mahomes")

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
    await create_matchup_candidates(session_factory, snapshot, min_edge=0.03)

    async with session_factory() as session:
        cand = (await session.execute(select(SteamCandidate))).scalars().one()
    assert "last_10" in cand.angles
    assert "vs_opponent" in cand.angles

    pick_id = await approve_steam_candidate(session_factory, cand.id, "alice")
    async with session_factory() as session:
        pick = (await session.execute(select(Pick).where(Pick.id == pick_id))).scalars().one()
        pick.result = "win"
        pick.profit_units = 1.0
        pick.clv = 0.015
        await session.commit()

    report = await angle_report(session_factory)

    assert report["last_10"]["count"] == 1
    assert report["last_10"]["avg_clv"] == pytest.approx(0.015)
    assert report["last_10"]["units"] == pytest.approx(1.0)


async def test_angle_report_ignores_ungraded_and_angleless_picks(session_factory):
    from fairline.matchup import angle_report

    assert await angle_report(session_factory) == {}


class TestParseGameContext:
    def test_home_team_gets_is_home_true(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Sunday,13:00,grass,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["is_home"] is True
        assert context[("BUF", 2025, 10)]["is_home"] is False

    def test_monday_and_thursday_are_primetime(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Monday,20:15,grass,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["is_primetime"] is True

    def test_late_sunday_is_primetime_early_sunday_is_not(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Sunday,20:20,grass,55,8\n"
            "2025,11,KC,BUF,Sunday,13:00,grass,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["is_primetime"] is True
        assert context[("KC", 2025, 11)]["is_primetime"] is False

    def test_bad_weather_flags_high_wind_or_cold(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Sunday,13:00,grass,55,20\n"
            "2025,11,KC,BUF,Sunday,13:00,grass,20,5\n"
            "2025,12,KC,BUF,Sunday,13:00,grass,60,5\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["bad_weather"] is True  # high wind
        assert context[("KC", 2025, 11)]["bad_weather"] is True  # cold
        assert context[("KC", 2025, 12)]["bad_weather"] is False

    def test_missing_temp_and_wind_is_no_bad_weather_data(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Sunday,13:00,grass,,\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["bad_weather"] is None

    def test_surface_value_is_stripped_of_stray_whitespace(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Sunday,13:00,grass ,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["surface"] == "grass"

    def test_weekday_value_is_stripped_of_stray_whitespace(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Monday ,20:15,grass,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["is_primetime"] is True

    def test_sunday_at_exactly_the_primetime_hour_is_primetime(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Sunday,19:00,grass,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["is_primetime"] is True

    def test_saturday_late_kickoff_is_not_primetime(self):
        from fairline.matchup import build_game_context

        csv_text = (
            "season,week,home_team,away_team,weekday,gametime,surface,temp,wind\n"
            "2025,10,KC,BUF,Saturday,20:15,grass,55,8\n"
        )
        context = build_game_context(csv_text)
        assert context[("KC", 2025, 10)]["is_primetime"] is False


class TestParsePlayerStatsWithContext:
    def test_context_lookup_populates_new_fields(self):
        stats_csv = (
            "season,week,player_display_name,team,opponent_team,position,passing_yards\n"
            "2025,10,Patrick Mahomes,KC,BUF,QB,310\n"
        )
        context_lookup = {
            ("KC", 2025, 10): {"is_home": True, "surface": "grass", "is_primetime": False, "bad_weather": False}
        }
        rows = parse_player_stats(stats_csv, context_lookup=context_lookup)
        assert rows[0].is_home is True
        assert rows[0].surface == "grass"
        assert rows[0].is_primetime is False
        assert rows[0].bad_weather is False

    def test_no_context_lookup_leaves_fields_null(self):
        stats_csv = (
            "season,week,player_display_name,team,opponent_team,position,passing_yards\n"
            "2025,10,Patrick Mahomes,KC,BUF,QB,310\n"
        )
        rows = parse_player_stats(stats_csv)
        assert rows[0].is_home is None
        assert rows[0].surface is None
