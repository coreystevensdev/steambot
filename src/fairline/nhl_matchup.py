"""NHL prop-matchup splits: home/away, back-to-back rest, and
vs-specific-opposing-goalie, on the same shrinkage-and-market-bound pattern
already proven for NFL and MLB props.

Reuses matchup.py's sport-agnostic combine_splits rather than duplicating
the blending math, exactly like the MLB feature does.

vs_goalie is the one split with a real small-sample risk (a skater can face
a specific opposing goalie only a handful of times a season), so it is the
only split with a hard floor: below MIN_VS_GOALIE_SAMPLE it is omitted from
the returned dict entirely.

vs_goalie is not wired into create_nhl_matchup_candidates below: knowing the
opposing starting goalie for an UPCOMING game needs pre-game confirmation
fairline doesn't have (the same probable-starter problem MLB's vs_pitcher
has). The function signature supports it for grading/backtest use against
already-completed games, where the goalie who actually started is known.

No day/night split exists for NHL; every game is played indoors.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from fairline.db.models import NhlPlayerGame
from fairline.matchup import combine_splits

logger = logging.getLogger(__name__)

NHL_PROP_STAT_COLUMNS = {
    "player_goals": "goals",
    "player_assists": "assists",
    "player_points": "points",
    "player_shots_on_goal": "shots_on_goal",
}

MIN_VS_GOALIE_SAMPLE = 10


def compute_nhl_prop_splits(
    games: list[NhlPlayerGame], stat: str, line: float, opposing_goalie: str | None = None
) -> dict[str, tuple[int, int]]:
    """Pre-registered NHL splits: last-N, season, home/away, back-to-back,
    and (when a real sample exists) vs the specific opposing goalie."""
    played = [g for g in games if getattr(g, stat) is not None]
    played.sort(key=lambda g: g.game_date, reverse=True)

    def rate(subset: list[NhlPlayerGame]) -> tuple[int, int]:
        hits = sum(1 for g in subset if getattr(g, stat) > line)
        return hits, len(subset)

    latest_season = played[0].season if played else 0
    splits: dict[str, tuple[int, int]] = {
        "last_5": rate(played[:5]),
        "last_10": rate(played[:10]),
        "season": rate([g for g in played if g.season == latest_season]),
        "home": rate([g for g in played if g.is_home is True]),
        "away": rate([g for g in played if g.is_home is False]),
        "back_to_back": rate([g for g in played if g.rest_days is not None and g.rest_days <= 1]),
    }

    if opposing_goalie:
        vs_goalie = rate([g for g in played if g.opposing_goalie == opposing_goalie])
        if vs_goalie[1] >= MIN_VS_GOALIE_SAMPLE:
            splits["vs_goalie"] = vs_goalie

    return splits


def nhl_matchup_probability(
    games: list[NhlPlayerGame], stat: str, line: float, side: str, market_fair: float,
    opposing_goalie: str | None = None,
) -> tuple[float, dict[str, tuple[int, int]]]:
    """Probability the side hits, with the splits that produced it."""
    splits = compute_nhl_prop_splits(games, stat, line, opposing_goalie)
    over_fair = market_fair if side == "Over" else 1 - market_fair
    over_prob = combine_splits(splits, base_rate=over_fair, market_fair=over_fair)
    return (over_prob if side == "Over" else 1 - over_prob), splits


def describe_nhl_splits(splits: dict[str, tuple[int, int]], side: str, line: float) -> str:
    parts = [
        f"{name} {hits}-{attempts - hits} over {line:g}"
        for name, (hits, attempts) in splits.items()
        if attempts > 0
    ]
    return f"{side} angles: " + "; ".join(parts)


async def create_nhl_matchup_candidates(session_factory, snapshot, min_edge: float = 0.03) -> int:
    """Queue NHL skater-prop candidates where the splits-adjusted number beats
    retail, mirroring the MLB and NFL matchup-candidate creation flow."""
    import uuid

    from fairline.clients.odds_api import RETAIL_BOOKS
    from fairline.db.models import SteamCandidate
    from fairline.props import _paired_outcomes, prop_fair_lines
    from fairline.state import american_to_prob

    fair_by_key = {
        (fl.market, fl.player, fl.point): fl.over_prob for fl in prop_fair_lines(snapshot)
    }
    if not fair_by_key:
        return 0

    created = 0
    async with session_factory() as session:
        for bm in snapshot.bookmakers:
            if bm.key not in RETAIL_BOOKS:
                continue
            for (market, player, point), pair in _paired_outcomes(snapshot, bm.key).items():
                over_fair = fair_by_key.get((market, player, point))
                stat = NHL_PROP_STAT_COLUMNS.get(market)
                if over_fair is None or stat is None:
                    continue
                games = (
                    (
                        await session.execute(
                            select(NhlPlayerGame).where(NhlPlayerGame.player == player)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not games:
                    continue
                for side in ("Over", "Under"):
                    market_fair = over_fair if side == "Over" else 1 - over_fair
                    prob, splits = nhl_matchup_probability(games, stat, point, side, market_fair)
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
                                + describe_nhl_splits(splits, side, point)
                            ),
                            angles=",".join(
                                name for name, (_, attempts) in splits.items() if attempts > 0
                            ),
                            source="nhl_matchup",
                            status="pending",
                        )
                    )
                    created += 1
        await session.commit()
    if created:
        logger.info("nhl_matchup: %d prop candidates pending review", created)
    return created
