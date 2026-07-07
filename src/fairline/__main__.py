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
    from fairline.clv import settle_closing_lines
    from fairline.db.session import get_session_factory

    async with httpx.AsyncClient() as client:
        games = await _fetch_odds_for(client, sport)
    summary = await settle_closing_lines(
        games, get_session_factory(), window_minutes=window_minutes
    )
    print(
        f"settled={summary['settled']} missed={summary['missed']} pending={summary['pending']}"
    )


async def _watch(interval_seconds: int, window_hours: float, once: bool, sport: str) -> None:
    import os
    from datetime import datetime, timezone

    from fairline.db.session import get_session_factory
    from fairline.steam import (
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
            print(
                f"{now.isoformat(timespec='seconds')} games={len(upcoming)} rows={written} steam={len(events)}"
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
    print(
        f"graded={summary['graded']} pending={summary['pending']} "
        f"missed={summary['missed']} results_recorded={results_recorded}"
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
    sub.add_parser("agents", help="per-agent leaderboard: record, avg CLV, units")
    backfill = sub.add_parser(
        "backfill-nfl", help="seed game_results (scores + closing lines) from nflverse"
    )
    backfill.add_argument(
        "--seasons", type=int, nargs="+", default=[2023, 2024, 2025],
        help="seasons to import (default: 2023 2024 2025)",
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
    elif args.command == "trends":
        asyncio.run(_trends(args.team, args.last))
    elif args.command == "backfill-nfl":
        asyncio.run(_backfill_nfl(args.seasons))
    elif args.command == "watch":
        asyncio.run(_watch(args.interval_seconds, args.window_hours, args.once, args.sport))


if __name__ == "__main__":
    main()
