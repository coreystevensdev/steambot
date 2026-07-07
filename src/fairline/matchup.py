"""The matchup engine (design: docs/matchup-agent-design.md).

The filter workflow, automated: pre-registered splits over player game logs,
beta-binomial shrinkage so short streaks read as evidence rather than truth,
and a final probability bounded near the market's fair number. Rationale is
assembled from the splits that carried weight; no probability comes from an
LLM.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from fairline.db.models import Pick, PlayerGame
from fairline.sim import NFL_TEAMS

logger = logging.getLogger(__name__)

PLAYER_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
)

PROP_STAT_COLUMNS = {
    "player_pass_yds": "passing_yards",
    "player_rush_yds": "rushing_yards",
    "player_reception_yds": "receiving_yards",
    "player_receptions": "receptions",
}

SHRINKAGE_K = 10
MAX_MARKET_DEVIATION = 0.06  # the matchup number may not stray further from fair
_TRACKED_POSITIONS = {"QB", "RB", "WR", "TE"}


def shrunk_probability(hits: int, attempts: int, base_rate: float, k: int = SHRINKAGE_K) -> float:
    """Beta-binomial shrinkage: small samples read mostly as the base rate."""
    return (hits + k * base_rate) / (attempts + k)


def compute_prop_splits(
    games: list[PlayerGame], stat: str, line: float, opponent: str | None = None
) -> dict[str, tuple[int, int]]:
    """Pre-registered splits only; searching for the best-looking slice is the
    multiple-comparisons trap this module exists to avoid."""
    played = [g for g in games if getattr(g, stat) is not None]
    played.sort(key=lambda g: (g.season, g.week), reverse=True)
    if opponent is None and played:
        opponent = played[0].opponent

    def rate(subset: list[PlayerGame]) -> tuple[int, int]:
        hits = sum(1 for g in subset if getattr(g, stat) > line)
        return hits, len(subset)

    latest_season = played[0].season if played else 0
    return {
        "last_5": rate(played[:5]),
        "last_10": rate(played[:10]),
        "season": rate([g for g in played if g.season == latest_season]),
        "vs_opponent": rate([g for g in played if g.opponent == opponent]),
    }


def combine_splits(
    splits: dict[str, tuple[int, int]], base_rate: float, market_fair: float
) -> float:
    """Precision-weighted blend of shrunk splits, clamped near the market.

    The market bound is the humility clause: a 10-for-10 streak may nudge the
    number, never rewrite it. Whatever the splits say, Pinnacle's devigged
    price has seen more information than ten box scores.
    """
    weighted = total_weight = 0.0
    for hits, attempts in splits.values():
        if attempts == 0:
            continue
        weight = attempts / (attempts + SHRINKAGE_K)
        weighted += weight * shrunk_probability(hits, attempts, base_rate)
        total_weight += weight
    if total_weight == 0:
        return market_fair
    raw = weighted / total_weight
    lo, hi = market_fair - MAX_MARKET_DEVIATION, market_fair + MAX_MARKET_DEVIATION
    return min(hi, max(lo, raw))


def matchup_probability(
    games: list[PlayerGame], stat: str, line: float, side: str, market_fair: float
) -> tuple[float, dict[str, tuple[int, int]]]:
    """Probability the side hits, with the splits that produced it."""
    splits = compute_prop_splits(games, stat, line)
    over_fair = market_fair if side == "Over" else 1 - market_fair
    over_prob = combine_splits(splits, base_rate=over_fair, market_fair=over_fair)
    return (over_prob if side == "Over" else 1 - over_prob), splits


def describe_splits(splits: dict[str, tuple[int, int]], side: str, line: float) -> str:
    parts = [
        f"{name} {hits}-{attempts - hits} over {line:g}"
        for name, (hits, attempts) in splits.items()
        if attempts > 0
    ]
    return f"{side} angles: " + "; ".join(parts)


def parse_player_stats(csv_text: str, date_lookup: dict | None = None) -> list[PlayerGame]:
    """Parse nflverse weekly player stats into rows worth keeping.

    Skill positions only, and only rows with at least one tracked stat; the
    table exists to answer prop questions, not to mirror nflverse.
    """
    date_lookup = date_lookup or {}
    rows: list[PlayerGame] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if row.get("position") not in _TRACKED_POSITIONS:
            continue
        # nflverse renamed recent_team to team in the stats_player release
        team_code = row.get("team") or row.get("recent_team") or ""
        team = NFL_TEAMS.get(team_code)
        opponent = NFL_TEAMS.get(row.get("opponent_team", ""))
        if not team or not opponent:
            continue

        def stat(key: str) -> float | None:
            try:
                return float(row[key])
            except (KeyError, TypeError, ValueError):
                return None

        stats = {col: stat(col) for col in PROP_STAT_COLUMNS.values()}
        if all(v is None for v in stats.values()):
            continue
        try:
            season, week = int(row["season"]), int(row["week"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            PlayerGame(
                sport="americanfootball_nfl",
                season=season,
                week=week,
                game_date=date_lookup.get((team_code, season, week)),
                player=row.get("player_display_name", ""),
                team=team,
                opponent=opponent,
                **stats,
            )
        )
    return rows


def _parse_prop_selection(selection: str) -> tuple[str, str, float] | None:
    """"Patrick Mahomes Over 275.5" -> (player, side, line)."""
    for side in (" Over ", " Under "):
        if side in selection:
            player, _, line_text = selection.partition(side)
            try:
                return player, side.strip(), float(line_text)
            except ValueError:
                return None
    return None


async def grade_prop_picks(session_factory) -> dict:
    """Grade prop picks against stored box scores.

    Matches by player name and game date within a day of the pick's
    commence_time; exact stat landings push, same as game totals.
    """
    graded = missed = 0
    async with session_factory() as session:
        picks = (
            (
                await session.execute(
                    select(Pick).where(
                        Pick.result.is_(None), Pick.market.in_(PROP_STAT_COLUMNS)
                    )
                )
            )
            .scalars()
            .all()
        )
        for pick in picks:
            parsed = _parse_prop_selection(pick.selection)
            stat_col = PROP_STAT_COLUMNS.get(pick.market)
            if parsed is None or stat_col is None or pick.commence_time is None:
                missed += 1
                continue
            player, side, line = parsed
            window = timedelta(days=1)
            rows = (
                (
                    await session.execute(
                        select(PlayerGame).where(
                            PlayerGame.player == player,
                            PlayerGame.game_date.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            commence = pick.commence_time
            if commence.tzinfo is None:
                from datetime import timezone as _tz

                commence = commence.replace(tzinfo=_tz.utc)
            match = None
            for r in rows:
                gd = r.game_date
                if gd.tzinfo is None:
                    from datetime import timezone as _tz

                    gd = gd.replace(tzinfo=_tz.utc)
                if abs(gd - commence) <= window and getattr(r, stat_col) is not None:
                    match = r
                    break
            if match is None:
                missed += 1
                logger.warning("grade props: no box score for pick_id=%s %r", pick.id, pick.selection)
                continue
            actual = getattr(match, stat_col)
            diff = (actual - line) if side == "Over" else (line - actual)
            if diff > 0:
                pick.result = "win"
                pick.profit_units = pick.price / 100 if pick.price > 0 else 100 / abs(pick.price)
            elif diff < 0:
                pick.result = "loss"
                pick.profit_units = -1.0
            else:
                pick.result = "push"
                pick.profit_units = 0.0
            graded += 1
        await session.commit()
    return {"graded": graded, "missed": missed}


async def create_matchup_candidates(
    session_factory, snapshot, min_edge: float = 0.03
) -> int:
    """Queue prop candidates where the matchup-adjusted number beats retail.

    The matchup probability starts from the sharp fair number and moves only
    as far as the bounded splits allow, so an edge here needs both a lagging
    retail price and supportive history. Candidates share the review queue
    with steam, tagged source="matchup".
    """
    import uuid

    from fairline.db.models import SteamCandidate
    from fairline.props import _paired_outcomes, prop_fair_lines
    from fairline.state import american_to_prob

    fair_by_key = {
        (fl.market, fl.player, fl.point): fl.over_prob for fl in prop_fair_lines(snapshot)
    }
    if not fair_by_key:
        return 0

    from fairline.clients.odds_api import RETAIL_BOOKS

    created = 0
    async with session_factory() as session:
        for bm in snapshot.bookmakers:
            if bm.key not in RETAIL_BOOKS:
                continue
            for (market, player, point), pair in _paired_outcomes(snapshot, bm.key).items():
                over_fair = fair_by_key.get((market, player, point))
                stat = PROP_STAT_COLUMNS.get(market)
                if over_fair is None or stat is None:
                    continue
                games = (
                    (
                        await session.execute(
                            select(PlayerGame).where(
                                PlayerGame.player == player,
                                PlayerGame.sport == snapshot.sport,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not games:
                    continue
                for side in ("Over", "Under"):
                    market_fair = over_fair if side == "Over" else 1 - over_fair
                    prob, splits = matchup_probability(games, stat, point, side, market_fair)
                    implied = american_to_prob(pair[side].price)
                    edge = prob - implied
                    if edge < min_edge:
                        continue
                    selection = f"{player} {side} {point:g}"
                    already = (
                        await session.execute(
                            select(SteamCandidate.id).where(
                                SteamCandidate.game_id == snapshot.game_id,
                                SteamCandidate.market == market,
                                SteamCandidate.selection == selection,
                                SteamCandidate.book == bm.key,
                                SteamCandidate.status == "pending",
                            )
                        )
                    ).scalar()
                    if already:
                        continue
                    price = pair[side].price
                    win_amount = price / 100 if price > 0 else 100 / abs(price)
                    session.add(
                        SteamCandidate(
                            id=str(uuid.uuid4()),
                            sport=snapshot.sport,
                            game_id=snapshot.game_id,
                            home_team=snapshot.home_team,
                            away_team=snapshot.away_team,
                            commence_time=snapshot.commence_time,
                            market=market,
                            selection=selection,
                            book=bm.key,
                            price=price,
                            sharp_probability=prob,
                            implied_probability=implied,
                            edge_pct=edge,
                            ev_pct=prob * win_amount - (1 - prob),
                            rationale=(
                                f"fair {market_fair:.3f} -> matchup {prob:.3f}; "
                                + describe_splits(splits, side, point)
                            ),
                            source="matchup",
                            status="pending",
                        )
                    )
                    created += 1
        await session.commit()
    if created:
        logger.info("matchup: %d prop candidates pending review", created)
    return created
