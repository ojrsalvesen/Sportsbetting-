from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from decimal import Decimal

from polymarket_bot.errors import BotError, ConfigError
from polymarket_bot.market_data import PolymarketPublicClient
from polymarket_bot.models import decimal_value
from polymarket_bot.reporting import (
    FREE_TIER_PINNACLE_REFRESH_SECONDS,
    REPORT_STAKE_CAP_USD,
    NoOddsProvider,
    TheOddsApiPinnacleProvider,
    build_premier_league_report,
    render_terminal,
)


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be numeric") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _stake(value: str) -> Decimal:
    try:
        parsed = decimal_value(value, field_name="stake")
    except ConfigError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if parsed <= 0 or parsed > REPORT_STAKE_CAP_USD:
        raise argparse.ArgumentTypeError(
            f"must be above 0 and no greater than {REPORT_STAKE_CAP_USD}"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="premier-league-report",
        description=(
            "Print read-only Premier League Polymarket bid/ask, spread, depth, "
            "and optional Pinnacle comparisons"
        ),
    )
    parser.add_argument(
        "--pinnacle-api",
        action="store_true",
        help="fetch Pinnacle EPL 1X2 odds through The Odds API free tier",
    )
    parser.add_argument(
        "--stake-usd",
        type=_stake,
        default=_stake(os.getenv("PMR_STAKE_USD", "5.00")),
        help="purchase size for visible-ask VWAP, hard-capped at $5",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=_positive_float(os.getenv("PMR_HTTP_TIMEOUT_SECONDS", "20")),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="refresh continuously and clear/redraw the terminal",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=_positive_float,
        default=_positive_float(os.getenv("PMR_REFRESH_SECONDS", "60")),
        help="Polymarket refresh interval in watch mode (default: 60 seconds)",
    )
    parser.add_argument(
        "--pinnacle-refresh-seconds",
        type=_positive_float,
        default=_positive_float(
            os.getenv(
                "PMR_PINNACLE_REFRESH_SECONDS",
                str(int(FREE_TIER_PINNACLE_REFRESH_SECONDS)),
            )
        ),
        help="Pinnacle refresh interval; free-only minimum/default is 5400 seconds",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="append each watch refresh instead of clearing the terminal",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        provider: NoOddsProvider | TheOddsApiPinnacleProvider
        if args.pinnacle_api:
            api_key = os.getenv("THE_ODDS_API_KEY", "").strip()
            if not api_key:
                api_key = getpass.getpass("The Odds API key (input hidden): ").strip()
            provider = TheOddsApiPinnacleProvider(
                api_key,
                timeout_seconds=args.timeout_seconds,
                refresh_seconds=args.pinnacle_refresh_seconds,
            )
        else:
            provider = NoOddsProvider()

        with PolymarketPublicClient(timeout_seconds=args.timeout_seconds) as client:
            first_cycle = True
            while True:
                try:
                    if isinstance(provider, TheOddsApiPinnacleProvider):
                        provider.refresh_if_due(force=first_cycle)
                    report = build_premier_league_report(
                        client,
                        odds_provider=provider,
                        stake_usd=args.stake_usd,
                    )
                    if args.watch and not args.no_clear:
                        print("\033[2J\033[H", end="")
                    print(render_terminal(report), flush=True)

                    book_count = sum(len(fixture.outcomes) for fixture in report.fixtures)
                    healthy_count = sum(
                        outcome.error is None
                        for fixture in report.fixtures
                        for outcome in fixture.outcomes
                    )
                    exit_code = 0 if report.fixtures and healthy_count == book_count else 2
                except BotError as error:
                    if not args.watch:
                        raise
                    if not args.no_clear:
                        print("\033[2J\033[H", end="")
                    print(f"Temporary refresh error: {error}", file=sys.stderr, flush=True)
                    exit_code = 2

                if not args.watch:
                    raise SystemExit(exit_code)
                first_cycle = False
                print(
                    f"\nRefreshing in {args.refresh_seconds:g} seconds; press Ctrl+C to stop.",
                    flush=True,
                )
                time.sleep(args.refresh_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0) from None
    except BotError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
