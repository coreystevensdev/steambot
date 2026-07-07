"""SteamBot CLI. Currently one job: closing-line settlement."""

from __future__ import annotations

import argparse
import asyncio
import logging

import httpx


async def _settle(window_minutes: int) -> None:
    from steambot.clients.odds_api import fetch_nfl_odds
    from steambot.clv import settle_closing_lines
    from steambot.db.session import get_session_factory

    async with httpx.AsyncClient() as client:
        games = await fetch_nfl_odds(client)
    summary = await settle_closing_lines(
        games, get_session_factory(), window_minutes=window_minutes
    )
    print(
        f"settled={summary['settled']} missed={summary['missed']} pending={summary['pending']}"
    )


async def _watch(interval_seconds: int, window_hours: float, once: bool) -> None:
    from datetime import datetime, timezone

    from steambot.clients.odds_api import fetch_nfl_odds
    from steambot.db.session import get_session_factory
    from steambot.steam import games_in_window, record_snapshots

    factory = get_session_factory()
    async with httpx.AsyncClient() as client:
        while True:
            now = datetime.now(timezone.utc)
            games = await fetch_nfl_odds(client)
            upcoming = games_in_window(games, now=now, window_hours=window_hours)
            if not upcoming:
                print(f"no games within {window_hours}h; exiting")
                return
            written = await record_snapshots(upcoming, factory, captured_at=now)
            print(f"{now.isoformat(timespec='seconds')} games={len(upcoming)} rows={written}")
            if once:
                return
            await asyncio.sleep(interval_seconds)


async def _sim_report(threshold: float) -> None:
    from steambot.clv import sim_clv_report
    from steambot.db.session import get_session_factory

    report = await sim_clv_report(get_session_factory(), disagree_threshold=threshold)
    for bucket, s in report.items():
        avg = f"{s['avg_clv']:+.4f}" if s["avg_clv"] is not None else "n/a"
        print(f"{bucket}: count={s['count']} avg_clv={avg} profit_units={s['profit_units']:+.2f}")


async def _create_user(email: str) -> None:
    from steambot.api.auth import issue_api_key
    from steambot.db.session import get_session_factory

    user_id, key = await issue_api_key(email, get_session_factory())
    print(f"user_id={user_id}")
    print(f"api_key={key}")
    print("Store the key now; only its hash is kept. Re-running rotates it.")


async def _grade(days_from: int) -> None:
    from steambot.clients.odds_api import fetch_nfl_scores
    from steambot.clv import grade_results
    from steambot.db.session import get_session_factory

    async with httpx.AsyncClient() as client:
        scores = await fetch_nfl_scores(client, days_from=days_from)
    summary = await grade_results(scores, get_session_factory())
    print(
        f"graded={summary['graded']} pending={summary['pending']} missed={summary['missed']}"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="steambot")
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
    grade = sub.add_parser(
        "grade", help="grade completed picks: win/loss/push and profit_units"
    )
    grade.add_argument(
        "--days-from",
        type=int,
        default=3,
        help="how many days back to fetch scores, max 3 (default 3)",
    )
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
        help="seconds between polls; each poll costs one API request (default 120)",
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
        asyncio.run(_settle(args.window_minutes))
    elif args.command == "grade":
        asyncio.run(_grade(args.days_from))
    elif args.command == "create-user":
        asyncio.run(_create_user(args.email))
    elif args.command == "sim-report":
        asyncio.run(_sim_report(args.threshold))
    elif args.command == "watch":
        asyncio.run(_watch(args.interval_seconds, args.window_hours, args.once))


if __name__ == "__main__":
    main()
