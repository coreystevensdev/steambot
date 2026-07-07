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
    args = parser.parse_args()
    if args.command == "settle":
        asyncio.run(_settle(args.window_minutes))


if __name__ == "__main__":
    main()
