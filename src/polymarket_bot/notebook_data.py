from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from polymarket_bot.analysis import analyze_bets
from polymarket_bot.errors import ConfigError
from polymarket_bot.history import (
    AnalyticsStore,
    is_season_epl_slug,
    season_bounds,
    validate_profile_address,
)


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
        records.append(
            {
                "trade_id": bet.trade.id,
                "asset": bet.trade.asset,
                "placed_at": bet.trade.placed_at,
                "event_slug": bet.trade.event_slug,
                "market_title": bet.trade.title,
                "fixture": event.title if event else None,
                "kickoff": event.kickoff if event else None,
                "home_team": event.home_team if event else None,
                "away_team": event.away_team if event else None,
                "market_role": bet.role,
                "selection": bet.token_side,
                "shares": float(bet.trade.size),
                "entry_price": float(bet.trade.price),
                "cost_usd": float(bet.trade.usdc_size),
                "result": bet.result,
                "hold_pnl_usd": float(bet.hold_pnl) if bet.hold_pnl is not None else None,
                "home_form_matches": bet.home_form.matches if bet.home_form else None,
                "home_form_xgf": float(bet.home_form.xg_for) if bet.home_form else None,
                "home_form_xga": float(bet.home_form.xg_against) if bet.home_form else None,
                "away_form_matches": bet.away_form.matches if bet.away_form else None,
                "away_form_xgf": float(bet.away_form.xg_for) if bet.away_form else None,
                "away_form_xga": float(bet.away_form.xg_against) if bet.away_form else None,
                "home_goals": match.home_goals if match else None,
                "away_goals": match.away_goals if match else None,
                "post_home_xg": float(match.home_xg) if match else None,
                "post_away_xg": float(match.away_xg) if match else None,
            }
        )
    return records


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
        trades = store.trades(address, season)
        events = store.events()
        matches = store.xg_matches(season - 1) + store.xg_matches(season)
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
        xg_matches = pd.read_sql_query(
            "SELECT * FROM xg_matches WHERE season IN (?, ?) ORDER BY kickoff_utc",
            connection,
            params=(season - 1, season),
        )

    if not raw_trades.empty:
        raw_trades["placed_at"] = pd.to_datetime(raw_trades["timestamp"], unit="s", utc=True)
        for column in ("size", "usdc_size", "price"):
            raw_trades[column] = pd.to_numeric(raw_trades[column], errors="coerce")
    if not closed_positions.empty:
        closed_positions = closed_positions[
            closed_positions.apply(
                lambda row: is_season_epl_slug(
                    str(row["event_slug"]), season, timestamp=int(row["timestamp"])
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
    if not xg_matches.empty:
        xg_matches["kickoff_utc"] = pd.to_datetime(xg_matches["kickoff_utc"], utc=True)
        for column in ("home_xg", "away_xg"):
            xg_matches[column] = pd.to_numeric(xg_matches[column], errors="coerce")

    bets = pd.DataFrame(_analysis_records(analyzed))
    return {
        "bets": bets,
        "trades": raw_trades,
        "closed_positions": closed_positions,
        "xg_matches": xg_matches,
        "profile_address": address,
        "database_path": path,
        "season": season,
    }
