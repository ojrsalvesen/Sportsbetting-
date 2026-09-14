from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from polymarket_bot.analysis import (
    AnalyticsReport,
    analyze_bets,
    render_analytics_terminal,
    xg_matches_for_events,
)
from polymarket_bot.errors import ApiError, BotError, ConfigError
from polymarket_bot.history import (
    AnalyticsStore,
    PolymarketHistoryClient,
    SUPPORTED_COMPETITIONS,
    is_season_competition_slug,
    validate_profile_address,
)
from polymarket_bot.xg import UnderstatXgClient


def _current_season() -> int:
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def _season(value: str) -> int:
    try:
        season = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a four-digit starting year") from error
    if not 2014 <= season <= 2100:
        raise argparse.ArgumentTypeError("must be between 2014 and 2100")
    return season


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be numeric") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _refresh_hours(value: str) -> float:
    parsed = _positive_float(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1 hour")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bet-analytics",
        description=(
            "Synchronize public Premier League and Champions League Polymarket trades into SQLite and "
            "review BUY fills with post-match xG for represented fixtures"
        ),
    )
    parser.add_argument(
        "--user",
        help="public Polymarket profile address (0x...); never use a private key",
    )
    parser.add_argument(
        "--season",
        type=_season,
        default=_current_season(),
        help="season starting year (default: current season)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("PMA_DATABASE_PATH", "data/bet_history.sqlite3")),
        help="local SQLite ledger (default: data/bet_history.sqlite3)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=_positive_float(os.getenv("PMA_HTTP_TIMEOUT_SECONDS", "20")),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="print from the local database without making network requests",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep syncing and redrawing the report (default: every 6 hours)",
    )
    parser.add_argument(
        "--refresh-hours",
        type=_refresh_hours,
        default=_refresh_hours(os.getenv("PMA_REFRESH_HOURS", "6")),
        help="watch-mode sync interval, minimum 1 hour (default: 6)",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="append watch-mode reports instead of clearing the terminal",
    )
    return parser


def _resolve_user(requested: str | None, store: AnalyticsStore) -> str:
    candidate = requested or os.getenv("POLYMARKET_PROFILE_ADDRESS", "").strip()
    if candidate:
        return validate_profile_address(candidate)
    known = store.known_users()
    if len(known) == 1:
        return known[0]
    if len(known) > 1:
        raise ConfigError("Multiple profiles are stored; choose one with --user")
    return validate_profile_address(input("Public Polymarket profile address (0x...): "))


def _run_cycle(args: argparse.Namespace, store: AnalyticsStore, user: str) -> str:
    warnings: list[str] = []
    if not args.offline:
        with PolymarketHistoryClient(timeout_seconds=args.timeout_seconds) as client:
            try:
                remote_trades = client.trades_for_season(
                    user, args.season, competitions=SUPPORTED_COMPETITIONS
                )
                inserted, received = store.upsert_trades(remote_trades)
                print(
                    f"Polymarket sync: {received} EPL/UCL trades received, "
                    f"{inserted} newly stored.",
                    flush=True,
                )
            except ApiError as error:
                if not store.trades(
                    user, args.season, competitions=SUPPORTED_COMPETITIONS
                ):
                    raise
                warnings.append(f"Trade sync failed; using cached data: {error}")

            try:
                positions = client.closed_positions_for_season(
                    user, args.season, competitions=SUPPORTED_COMPETITIONS
                )
                store.upsert_closed_positions(positions)
            except ApiError as error:
                warnings.append(f"Closed-position sync failed: {error}")

            local_trades = store.trades(
                user, args.season, competitions=SUPPORTED_COMPETITIONS
            )
            cached_events = store.events()
            fixture_conditions: dict[str, set[str]] = {}
            for trade in local_trades:
                if trade.side != "BUY" or not is_season_competition_slug(
                    trade.event_slug,
                    args.season,
                    competitions=SUPPORTED_COMPETITIONS,
                ):
                    continue
                fixture_conditions.setdefault(trade.event_slug, set()).add(
                    trade.condition_id
                )
            refresh_slugs = sorted(
                slug
                for slug, condition_ids in fixture_conditions.items()
                if slug not in cached_events
                or not condition_ids.issubset(cached_events[slug].winning_outcomes)
            )
            for event_slug in refresh_slugs:
                try:
                    store.upsert_event(client.event(event_slug))
                except BotError as error:
                    warnings.append(f"Event metadata unavailable for {event_slug}: {error}")

        local_trades = store.trades(
            user, args.season, competitions=SUPPORTED_COMPETITIONS
        )
        cached_events = store.events()
        traded_slugs = {
            trade.event_slug for trade in local_trades if trade.side == "BUY"
        }
        bet_events = [
            cached_events[slug]
            for slug in traded_slugs
            if slug in cached_events
        ]
        synced_matches = 0
        if bet_events:
            try:
                with UnderstatXgClient(timeout_seconds=args.timeout_seconds) as xg_client:
                    remote_matches = xg_client.matches(args.season)
                relevant_matches = xg_matches_for_events(bet_events, remote_matches)
                synced_matches = store.upsert_xg_matches(relevant_matches)
            except ApiError as error:
                warnings.append(
                    f"{args.season}/{str(args.season + 1)[-2:]} xG sync failed; "
                    f"using cached data: {error}"
                )
        print(
            f"xG sync: {synced_matches} completed bet fixtures stored/refreshed "
            f"for {args.season}/{str(args.season + 1)[-2:]}.",
            flush=True,
        )

    trades = store.trades(user, args.season, competitions=SUPPORTED_COMPETITIONS)
    events = store.events()
    traded_slugs = {trade.event_slug for trade in trades if trade.side == "BUY"}
    bet_events = [events[slug] for slug in traded_slugs if slug in events]
    matches = xg_matches_for_events(bet_events, store.xg_matches(args.season))
    position_count, realized_pnl = store.realized_pnl(
        user, args.season, competitions=SUPPORTED_COMPETITIONS
    )
    if trades and not events:
        warnings.append("No event metadata is available; market roles cannot be analyzed")
    if bet_events and not matches:
        warnings.append("No completed xG is available yet for represented bet fixtures")
    analyzed = analyze_bets(trades, events, matches)
    unclassified_completed = sum(
        bet.match is not None and bet.result == "PENDING" for bet in analyzed
    )
    if unclassified_completed:
        warnings.append(
            f"{unclassified_completed} BUY fills from completed fixtures could not be "
            "classified; inspect their market rules before using hold P/L"
        )
    report = AnalyticsReport(
        generated_at=datetime.now(timezone.utc),
        season=args.season,
        user=user,
        database_path=str(args.database.resolve()),
        trades_stored=len(trades),
        closed_positions=position_count,
        realized_pnl=realized_pnl,
        bets=tuple(analyzed),
        warnings=tuple(warnings),
    )
    return render_analytics_terminal(report)


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.offline and args.watch:
        parser.error("--offline and --watch cannot be used together")
    try:
        with AnalyticsStore(args.database) as store:
            user = _resolve_user(args.user, store)
            store.remember_user(user)
            while True:
                if args.watch and not args.no_clear:
                    print("\033[2J\033[H", end="")
                print(_run_cycle(args, store, user), flush=True)
                if not args.watch:
                    break
                print(
                    f"\nRefreshing in {args.refresh_hours:g} hours; press Ctrl+C to stop.",
                    flush=True,
                )
                time.sleep(args.refresh_hours * 3600)
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0) from None
    except (BotError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
