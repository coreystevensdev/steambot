"""Fairline CLI: settlement, grading, line watching, users, and agent records."""

from __future__ import annotations

import argparse
import asyncio
import logging

import httpx

SPORT_CHOICES = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
]


async def _fetch_odds_for(client: httpx.AsyncClient, sport: str):
    from fairline.clients.odds_api import fetch_odds

    sports = SPORT_CHOICES if sport == "all" else [sport]
    games = []
    for s in sports:
        games.extend(await fetch_odds(client, s))
    return games


async def _settle(window_minutes: int, sport: str) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from fairline.clients.odds_api import fetch_event_props
    from fairline.clv import settle_closing_lines
    from fairline.db.models import Pick
    from fairline.db.session import get_session_factory
    from fairline.matchup import PROP_STAT_COLUMNS
    from fairline.props import settle_prop_picks

    factory = get_session_factory()
    async with httpx.AsyncClient() as client:
        games = await _fetch_odds_for(client, sport)
        summary = await settle_closing_lines(games, factory, window_minutes=window_minutes)

        # prop closing lines cost one request per event; fetch only events
        # that actually hold an unsettled prop pick inside the window
        cutoff = datetime.now(timezone.utc) + timedelta(minutes=window_minutes)
        async with factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(Pick.game_id, Pick.sport, Pick.commence_time)
                        .where(
                            Pick.closing_price.is_(None),
                            Pick.market.in_(PROP_STAT_COLUMNS),
                        )
                        .distinct()
                    )
                )
                .all()
            )
        # window filter in Python: naive-vs-timestamptz SQL comparisons behave
        # differently across SQLite and Postgres, the same trap clv.py sidesteps
        rows = []
        for game_id, pick_sport, commence in candidates:
            if commence is None:
                continue
            if commence.tzinfo is None:
                commence = commence.replace(tzinfo=timezone.utc)
            if commence <= cutoff:
                rows.append((game_id, pick_sport))
        snapshots = []
        for game_id, pick_sport in rows:
            snap = await fetch_event_props(client, pick_sport, game_id)
            if snap is not None:
                snapshots.append(snap)
    prop_summary = await settle_prop_picks(snapshots, factory, window_minutes=window_minutes)
    print(
        f"settled={summary['settled']} missed={summary['missed']} pending={summary['pending']} "
        f"props_settled={prop_summary['settled']} props_point_moved={prop_summary['point_moved']} "
        f"props_missed={prop_summary['missed']}"
    )


async def _watch(interval_seconds: int, window_hours: float, once: bool, sport: str) -> None:
    import os
    from datetime import datetime, timezone

    from fairline.db.session import get_session_factory
    from fairline.steam import (
        create_steam_candidates,
        format_steam_event,
        games_in_window,
        record_snapshots,
        scan_recent_steam,
    )

    factory = get_session_factory()
    webhook_url = os.environ.get("FAIRLINE_WEBHOOK_URL", "")
    async with httpx.AsyncClient() as client:
        while True:
            now = datetime.now(timezone.utc)
            games = await _fetch_odds_for(client, sport)
            upcoming = games_in_window(games, now=now, window_hours=window_hours)
            if not upcoming:
                print(f"no games within {window_hours}h; exiting")
                return
            written = await record_snapshots(upcoming, factory, captured_at=now)
            events = await scan_recent_steam(factory)
            pending = await create_steam_candidates(factory, events, upcoming) if events else 0
            print(
                f"{now.isoformat(timespec='seconds')} games={len(upcoming)} rows={written} "
                f"steam={len(events)} candidates={pending}"
            )
            for event in events:
                line = format_steam_event(event)
                print(line)
                if webhook_url:
                    try:
                        await client.post(webhook_url, json={"text": line}, timeout=10.0)
                    except httpx.HTTPError as exc:
                        logging.getLogger(__name__).warning("webhook post failed: %s", exc)
            if once:
                return
            await asyncio.sleep(interval_seconds)


async def _grade(days_from: int, sport: str) -> None:
    from fairline.clients.odds_api import fetch_scores
    from fairline.clv import grade_results
    from fairline.db.session import get_session_factory

    from fairline.trends import record_game_results

    factory = get_session_factory()
    sports = SPORT_CHOICES if sport == "all" else [sport]
    scores = []
    results_recorded = 0
    async with httpx.AsyncClient() as client:
        for s in sports:
            league_scores = await fetch_scores(client, s, days_from=days_from)
            scores.extend(league_scores)
            results_recorded += await record_game_results(league_scores, factory, sport=s)
    summary = await grade_results(scores, factory)
    from fairline.matchup import grade_prop_picks

    props = await grade_prop_picks(factory)
    print(
        f"graded={summary['graded']} pending={summary['pending']} "
        f"missed={summary['missed']} results_recorded={results_recorded} "
        f"props_graded={props['graded']} props_missed={props['missed']}"
    )


async def _backfill_nfl(seasons: list[int]) -> None:
    from fairline.db.session import get_session_factory
    from fairline.sim import NFLVERSE_GAMES_URL, parse_nflverse_games

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(NFLVERSE_GAMES_URL, timeout=60.0)
        resp.raise_for_status()
    _, results = parse_nflverse_games(resp.text)
    wanted = [r for r in results if int(r.game_id.split("_")[0]) in set(seasons)]

    factory = get_session_factory()
    async with factory() as session:
        for r in wanted:
            await session.merge(r)
        await session.commit()
    print(f"backfilled {len(wanted)} games for seasons {sorted(set(seasons))}")


async def _backfill_players(seasons: list[int]) -> None:
    from sqlalchemy import delete

    from fairline.db.models import PlayerGame
    from fairline.db.session import get_session_factory
    from fairline.matchup import PLAYER_STATS_URL, parse_player_stats
    from fairline.sim import NFLVERSE_GAMES_URL

    async with httpx.AsyncClient(follow_redirects=True) as client:
        games_resp = await client.get(NFLVERSE_GAMES_URL, timeout=60.0)
        games_resp.raise_for_status()
        import csv as _csv
        import io as _io
        from datetime import datetime as _dt

        date_lookup = {}
        for row in _csv.DictReader(_io.StringIO(games_resp.text)):
            try:
                season, week = int(row["season"]), int(row["week"])
                gameday = _dt.fromisoformat(row["gameday"])
            except (KeyError, TypeError, ValueError):
                continue
            date_lookup[(row.get("home_team"), season, week)] = gameday
            date_lookup[(row.get("away_team"), season, week)] = gameday

        from fairline.matchup import build_game_context

        context_lookup = build_game_context(games_resp.text)

        factory = get_session_factory()
        total = 0
        for season in sorted(set(seasons)):
            resp = await client.get(PLAYER_STATS_URL.format(season=season), timeout=120.0)
            resp.raise_for_status()
            rows = parse_player_stats(resp.text, date_lookup, context_lookup)
            async with factory() as session:
                await session.execute(
                    delete(PlayerGame).where(
                        PlayerGame.sport == "americanfootball_nfl",
                        PlayerGame.season == season,
                    )
                )
                session.add_all(rows)
                await session.commit()
            total += len(rows)
            print(f"season {season}: {len(rows)} player games")
        print(f"backfilled {total} player games")


async def _backfill_mlb_players(start_date: str, end_date: str) -> None:
    from fairline.db.session import get_session_factory
    from fairline.mlb_stats import fetch_mlb_batter_games

    rows = await fetch_mlb_batter_games(start_date, end_date)
    factory = get_session_factory()
    async with factory() as session:
        session.add_all(rows)
        await session.commit()
    print(f"backfilled {len(rows)} MLB batter games from {start_date} to {end_date}")


async def _backfill_nhl_players(team: str, season: str) -> None:
    from fairline.db.session import get_session_factory
    from fairline.nhl_stats import fetch_nhl_skater_games

    factory = get_session_factory()
    async with httpx.AsyncClient() as client:
        rows = await fetch_nhl_skater_games(client, team, season)
    async with factory() as session:
        session.add_all(rows)
        await session.commit()
    print(f"backfilled {len(rows)} NHL skater games for {team} season {season}")


async def _matchup(sport: str, markets: str, min_edge: float, max_events: int) -> None:
    from datetime import datetime, timezone

    from fairline.clients.odds_api import fetch_event_props, fetch_odds
    from fairline.db.session import get_session_factory

    if sport == "baseball_mlb":
        from fairline.mlb_matchup import create_mlb_matchup_candidates as create_candidates
    elif sport == "icehockey_nhl":
        from fairline.nhl_matchup import create_nhl_matchup_candidates as create_candidates
    else:
        from fairline.matchup import create_matchup_candidates as create_candidates

    factory = get_session_factory()
    async with httpx.AsyncClient() as client:
        games = await fetch_odds(client, sport)
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            (g for g in games if g.commence_time > now), key=lambda g: g.commence_time
        )[:max_events]
        if not upcoming:
            print("no upcoming events")
            return
        created = 0
        for game in upcoming:
            snap = await fetch_event_props(client, sport, game.game_id, markets=markets)
            if snap is not None:
                created += await create_candidates(factory, snap, min_edge=min_edge)
    print(f"events={len(upcoming)} candidates={created} (review at GET /api/steam)")


async def _trends(team: str, last_n: int) -> None:
    from sqlalchemy import or_, select

    from fairline.db.models import GameResult
    from fairline.db.session import get_session_factory
    from fairline.trends import compute_team_trends

    factory = get_session_factory()
    async with factory() as session:
        results = (
            (
                await session.execute(
                    select(GameResult).where(
                        or_(GameResult.home_team == team, GameResult.away_team == team)
                    )
                )
            )
            .scalars()
            .all()
        )
    t = compute_team_trends(results, team, last_n=last_n)
    print(f"{team}: {t['su']} SU, {t['ats']} ATS, {t['ou']} O/U (last {t['n']})")


async def _props(sport: str, markets: str, min_edge: float, max_events: int) -> None:
    from datetime import datetime, timezone

    from fairline.clients.odds_api import fetch_event_props, fetch_odds
    from fairline.props import find_prop_edges

    async with httpx.AsyncClient() as client:
        games = await fetch_odds(client, sport)
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            (g for g in games if g.commence_time > now), key=lambda g: g.commence_time
        )[:max_events]
        if not upcoming:
            print("no upcoming events")
            return
        total_edges = 0
        for game in upcoming:
            snap = await fetch_event_props(client, sport, game.game_id, markets=markets)
            if snap is None:
                continue
            for e in find_prop_edges(snap, min_edge=min_edge):
                total_edges += 1
                print(
                    f"{game.away_team} @ {game.home_team}  {e.player} {e.side} {e.point:g} "
                    f"({e.market}) {e.price:+d} at {e.book}  fair={e.fair_prob:.3f} edge={e.edge_pct:+.3f}"
                )
        print(f"events={len(upcoming)} edges={total_edges}")


async def _angles() -> None:
    from fairline.db.session import get_session_factory
    from fairline.matchup import angle_report

    report = await angle_report(get_session_factory())
    if not report:
        print("no graded matchup picks yet")
        return
    for angle, s in sorted(report.items()):
        clv = f"{s['avg_clv']:+.4f}" if s["avg_clv"] is not None else "n/a"
        print(
            f"{angle}: {s['wins']}-{s['losses']} units={s['units']:+.2f} "
            f"avg_clv={clv} n={s['count']}"
        )


async def _agents() -> None:
    from fairline.clv import agent_report
    from fairline.db.session import get_session_factory

    report = await agent_report(get_session_factory())
    if not report:
        print("no settled picks yet")
        return
    for source, s in report.items():
        print(
            f"{source}: {s['record']} avg_clv={s['avg_clv']:+.4f} "
            f"units={s['profit_units']:+.2f} n={s['count']}"
        )


async def _sim_report(threshold: float) -> None:
    from fairline.clv import sim_clv_report
    from fairline.db.session import get_session_factory

    report = await sim_clv_report(get_session_factory(), disagree_threshold=threshold)
    for bucket, s in report.items():
        avg = f"{s['avg_clv']:+.4f}" if s["avg_clv"] is not None else "n/a"
        print(f"{bucket}: count={s['count']} avg_clv={avg} profit_units={s['profit_units']:+.2f}")


async def _create_user(email: str) -> None:
    from fairline.api.auth import issue_api_key
    from fairline.db.session import get_session_factory

    user_id, key = await issue_api_key(email, get_session_factory())
    print(f"user_id={user_id}")
    print(f"api_key={key}")
    print("Store the key now; only its hash is kept. Re-running rotates it.")


def _add_sport_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sport",
        choices=SPORT_CHOICES + ["all"],
        default="americanfootball_nfl",
        help="league to operate on; 'all' costs one API request per league",
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="fairline")
    sub = parser.add_subparsers(dest="command", required=True)
    settle = sub.add_parser(
        "settle", help="capture closing lines and compute CLV for unsettled picks"
    )
    settle.add_argument(
        "--window-minutes",
        type=int,
        default=30,
        help="settle picks whose game starts within this many minutes (default 30)",
    )
    _add_sport_arg(settle)
    grade = sub.add_parser(
        "grade", help="grade completed picks: win/loss/push and profit_units"
    )
    grade.add_argument(
        "--days-from",
        type=int,
        default=3,
        help="how many days back to fetch scores, max 3 (default 3)",
    )
    _add_sport_arg(grade)
    create_user = sub.add_parser(
        "create-user", help="create a user (or rotate their key) and print the API key once"
    )
    create_user.add_argument("--email", required=True)
    watch = sub.add_parser(
        "watch", help="poll and store line snapshots for games near kickoff"
    )
    watch.add_argument(
        "--interval-seconds",
        type=int,
        default=120,
        help="seconds between polls; each poll costs one API request per league (default 120)",
    )
    watch.add_argument(
        "--window-hours",
        type=float,
        default=3.0,
        help="track games starting within this many hours (default 3)",
    )
    watch.add_argument(
        "--once", action="store_true", help="poll a single cycle and exit (cron mode)"
    )
    _add_sport_arg(watch)
    props = sub.add_parser(
        "props", help="scan player props for retail prices lagging the sharp fair number"
    )
    _add_sport_arg(props)
    props.add_argument(
        "--markets",
        default="player_pass_yds,player_rush_yds,player_reception_yds,player_receptions",
        help="comma-separated prop market keys",
    )
    props.add_argument("--min-edge", type=float, default=0.03)
    props.add_argument(
        "--max-events",
        type=int,
        default=5,
        help="props cost one API request per event; this caps the spend per scan (default 5)",
    )
    sub.add_parser("agents", help="per-agent leaderboard: record, avg CLV, units")
    sub.add_parser("angles", help="per-angle records over graded matchup picks")
    backfill = sub.add_parser(
        "backfill-nfl", help="seed game_results (scores + closing lines) from nflverse"
    )
    backfill.add_argument(
        "--seasons", type=int, nargs="+", default=[2023, 2024, 2025],
        help="seasons to import (default: 2023 2024 2025)",
    )
    backfill_players = sub.add_parser(
        "backfill-players", help="seed player_games from nflverse weekly stats"
    )
    backfill_players.add_argument(
        "--seasons", type=int, nargs="+", default=[2023, 2024, 2025],
        help="seasons to import (default: 2023 2024 2025)",
    )
    backfill_mlb = sub.add_parser(
        "backfill-mlb-players", help="ingest per-game MLB batter stats via pybaseball"
    )
    backfill_mlb.add_argument("--start", required=True, help="YYYY-MM-DD")
    backfill_mlb.add_argument("--end", required=True, help="YYYY-MM-DD")
    backfill_nhl = sub.add_parser(
        "backfill-nhl-players", help="ingest per-game NHL skater stats via the NHL's official API"
    )
    backfill_nhl.add_argument("--team", required=True, help="3-letter team code, e.g. EDM")
    backfill_nhl.add_argument("--season", required=True, help="8-digit season code, e.g. 20252026")
    matchup = sub.add_parser(
        "matchup", help="queue prop candidates where history-adjusted numbers beat retail"
    )
    _add_sport_arg(matchup)
    matchup.add_argument(
        "--markets",
        default="player_pass_yds,player_rush_yds,player_reception_yds,player_receptions",
    )
    matchup.add_argument("--min-edge", type=float, default=0.03)
    matchup.add_argument(
        "--max-events", type=int, default=5,
        help="props cost one API request per event; this caps the spend (default 5)",
    )
    trends = sub.add_parser("trends", help="SU/ATS/O-U record for a team from stored results")
    trends.add_argument("--team", required=True)
    trends.add_argument("--last", type=int, default=10, help="window size in games (default 10)")
    sim_report = sub.add_parser(
        "sim-report", help="compare CLV on picks where the sim agreed vs disagreed with the market"
    )
    sim_report.add_argument(
        "--threshold",
        type=float,
        default=0.02,
        help="minimum |sim - sharp| probability gap to count as disagreement (default 0.02)",
    )
    args = parser.parse_args()
    if args.command == "settle":
        asyncio.run(_settle(args.window_minutes, args.sport))
    elif args.command == "grade":
        asyncio.run(_grade(args.days_from, args.sport))
    elif args.command == "create-user":
        asyncio.run(_create_user(args.email))
    elif args.command == "sim-report":
        asyncio.run(_sim_report(args.threshold))
    elif args.command == "agents":
        asyncio.run(_agents())
    elif args.command == "angles":
        asyncio.run(_angles())
    elif args.command == "trends":
        asyncio.run(_trends(args.team, args.last))
    elif args.command == "backfill-nfl":
        asyncio.run(_backfill_nfl(args.seasons))
    elif args.command == "props":
        asyncio.run(_props(args.sport, args.markets, args.min_edge, args.max_events))
    elif args.command == "backfill-players":
        asyncio.run(_backfill_players(args.seasons))
    elif args.command == "backfill-mlb-players":
        asyncio.run(_backfill_mlb_players(args.start, args.end))
    elif args.command == "backfill-nhl-players":
        asyncio.run(_backfill_nhl_players(args.team, args.season))
    elif args.command == "matchup":
        asyncio.run(_matchup(args.sport, args.markets, args.min_edge, args.max_events))
    elif args.command == "watch":
        asyncio.run(_watch(args.interval_seconds, args.window_hours, args.once, args.sport))


if __name__ == "__main__":
    main()
