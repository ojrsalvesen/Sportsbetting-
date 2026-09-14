from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from polymarket_bot.analysis import analyze_bets, xg_matches_for_events
from polymarket_bot.errors import ConfigError
from polymarket_bot.history import (
    AnalyticsStore,
    SUPPORTED_COMPETITIONS,
    is_season_competition_slug,
    season_bounds,
    validate_profile_address,
)


BET_COLUMNS = [
    "trade_id",
    "selection_key",
    "asset",
    "placed_at",
    "event_slug",
    "market_title",
    "fixture",
    "kickoff",
    "home_team",
    "away_team",
    "market_type",
    "market_role",
    "selection",
    "direction_team",
    "direction_opponent",
    "shares",
    "entry_price",
    "cost_usd",
    "result",
    "resolution_source",
    "hold_pnl_usd",
    "match_id",
    "home_goals",
    "away_goals",
    "post_home_xg",
    "post_away_xg",
    "direction_goals",
    "opponent_goals",
    "direction_xg",
    "opponent_xg",
    "direction_xg_diff",
]

XG_COLUMNS = [
    "match_id",
    "season",
    "kickoff_utc",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "home_xg",
    "away_xg",
    "source",
]


def _pandas() -> Any:
    try:
        import pandas as pd
    except ImportError as error:
        raise ConfigError(
            'Notebook dependencies are missing; install with pip install -e ".[notebook]"'
        ) from error
    return pd


def _resolve_user(store: AnalyticsStore, user: str | None) -> str:
    if user:
        return validate_profile_address(user)
    known = store.known_users()
    if len(known) == 1:
        return known[0]
    if not known:
        raise ConfigError("No profile is stored; run bet-analytics once before the notebook")
    raise ConfigError("Multiple profiles are stored; pass user='0x...' explicitly")


def _analysis_records(bets: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for bet in bets:
        event = bet.event
        match = bet.match
        direction_goals = None
        opponent_goals = None
        direction_xg = None
        opponent_xg = None
        if match is not None and bet.direction_is_home is not None:
            if bet.direction_is_home:
                direction_goals, opponent_goals = match.home_goals, match.away_goals
                direction_xg, opponent_xg = match.home_xg, match.away_xg
            else:
                direction_goals, opponent_goals = match.away_goals, match.home_goals
                direction_xg, opponent_xg = match.away_xg, match.home_xg
        records.append(
            {
                "trade_id": bet.trade.id,
                "selection_key": bet.trade.asset
                or f"{bet.trade.condition_id}:{bet.trade.outcome_index}",
                "asset": bet.trade.asset,
                "placed_at": bet.trade.placed_at,
                "event_slug": bet.trade.event_slug,
                "market_title": bet.trade.title,
                "fixture": event.title if event else None,
                "kickoff": event.kickoff if event else None,
                "home_team": event.home_team if event else None,
                "away_team": event.away_team if event else None,
                "market_type": bet.market_type,
                "market_role": bet.role,
                "selection": bet.token_side,
                "direction_team": bet.direction_team,
                "direction_opponent": bet.direction_opponent,
                "shares": float(bet.trade.size),
                "entry_price": float(bet.trade.price),
                "cost_usd": float(bet.trade.usdc_size),
                "result": bet.result,
                "resolution_source": bet.resolution_source,
                "hold_pnl_usd": float(bet.hold_pnl) if bet.hold_pnl is not None else None,
                "match_id": match.match_id if match else None,
                "home_goals": match.home_goals if match else None,
                "away_goals": match.away_goals if match else None,
                "post_home_xg": float(match.home_xg) if match else None,
                "post_away_xg": float(match.away_xg) if match else None,
                "direction_goals": direction_goals,
                "opponent_goals": opponent_goals,
                "direction_xg": float(direction_xg) if direction_xg is not None else None,
                "opponent_xg": float(opponent_xg) if opponent_xg is not None else None,
                "direction_xg_diff": (
                    float(direction_xg - opponent_xg)
                    if direction_xg is not None and opponent_xg is not None
                    else None
                ),
            }
        )
    return records


def _xg_records(matches: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "match_id": match.match_id,
            "season": match.season,
            "kickoff_utc": match.kickoff,
            "home_team": match.home_team,
            "away_team": match.away_team,
            "home_goals": match.home_goals,
            "away_goals": match.away_goals,
            "home_xg": float(match.home_xg),
            "away_xg": float(match.away_xg),
            "source": match.source,
        }
        for match in matches
    ]


def load_notebook_data(
    database: str | Path = "data/bet_history.sqlite3",
    *,
    season: int,
    user: str | None = None,
) -> dict[str, Any]:
    """Load privacy-local analytics tables and an enriched BUY-bet DataFrame."""
    pd = _pandas()
    path = Path(database).resolve()
    if not path.is_file():
        raise ConfigError(f"Analytics database does not exist: {path}")

    with AnalyticsStore(path) as store:
        address = _resolve_user(store, user)
        trades = store.trades(address, season, competitions=SUPPORTED_COMPETITIONS)
        events = store.events()
        bet_events = {
            trade.event_slug: events[trade.event_slug]
            for trade in trades
            if trade.side == "BUY" and trade.event_slug in events
        }
        matches = xg_matches_for_events(
            list(bet_events.values()), store.xg_matches(season)
        )
        analyzed = analyze_bets(trades, events, matches)

    start, end = season_bounds(season)
    uri = f"file:{path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        raw_trades = pd.read_sql_query(
            """
            SELECT * FROM trades
            WHERE user = ? AND timestamp >= ? AND timestamp < ?
            ORDER BY timestamp
            """,
            connection,
            params=(address, int(start.timestamp()), int(end.timestamp())),
        )
        closed_positions = pd.read_sql_query(
            "SELECT * FROM closed_positions WHERE user = ? ORDER BY timestamp",
            connection,
            params=(address,),
        )

    if not raw_trades.empty:
        raw_trades = raw_trades[
            raw_trades.apply(
                lambda row: is_season_competition_slug(
                    str(row["event_slug"]), season,
                    competitions=SUPPORTED_COMPETITIONS,
                    timestamp=int(row["timestamp"]),
                ),
                axis=1,
            )
        ].copy()
    raw_trades["placed_at"] = pd.to_datetime(raw_trades["timestamp"], unit="s", utc=True)
    for column in ("size", "usdc_size", "price"):
        raw_trades[column] = pd.to_numeric(raw_trades[column], errors="coerce")
    if not closed_positions.empty:
        closed_positions = closed_positions[
            closed_positions.apply(
                lambda row: is_season_competition_slug(
                    str(row["event_slug"]), season,
                    competitions=SUPPORTED_COMPETITIONS,
                    timestamp=int(row["timestamp"]),
                ),
                axis=1,
            )
        ].copy()
    closed_positions["closed_at"] = pd.to_datetime(
        closed_positions["timestamp"], unit="s", utc=True
    )
    for column in ("average_price", "total_bought", "realized_pnl"):
        closed_positions[column] = pd.to_numeric(
            closed_positions[column], errors="coerce"
        )
    bets = pd.DataFrame(_analysis_records(analyzed), columns=BET_COLUMNS)
    xg_matches = pd.DataFrame(_xg_records(matches), columns=XG_COLUMNS)
    bets["placed_at"] = pd.to_datetime(bets["placed_at"], utc=True)
    bets["kickoff"] = pd.to_datetime(bets["kickoff"], utc=True)
    xg_matches["kickoff_utc"] = pd.to_datetime(xg_matches["kickoff_utc"], utc=True)
    return {
        "bets": bets,
        "trades": raw_trades,
        "closed_positions": closed_positions,
        "xg_matches": xg_matches,
        "profile_address": address,
        "database_path": path,
        "season": season,
    }
