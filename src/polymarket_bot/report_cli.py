from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from decimal import Decimal

from polymarket_bot.errors import BotError, ConfigError
from polymarket_bot.market_data import PolymarketPublicClient
from polymarket_bot.models import decimal_value
from polymarket_bot.competitions import build_market_reports
from polymarket_bot.odds_audit import OddsRequestBudget
from polymarket_bot.reporting import (
    REPORT_STAKE_CAP_USD,
    TheOddsApiPinnacleProvider,
    render_terminal,
    SPORT_KEYS,
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
            "Fetch once and print read-only EPL and EPL-team Champions League Polymarket markets "
            "with optional Pinnacle fair-price comparisons"
        ),
    )
    parser.add_argument(
        "--pinnacle-api",
        action="store_true",
        help="fetch Pinnacle 90-minute 1X2 and exact-handicap odds for the selected competitions",
    )
    parser.add_argument("--competitions", choices=["epl", "ucl", "both"],
                        default=os.getenv("PMR_COMPETITIONS", "both"),
                        help="competitions to show (default: both); UCL is limited to current EPL teams")
    parser.add_argument("--odds-snapshots", type=Path,
                        default=Path("data/odds_snapshots.sqlite3"),
                        help="local credential-free timestamped Pinnacle quote archive")
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
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        competitions = ("epl", "ucl") if args.competitions == "both" else (args.competitions,)
        providers = {}
        if args.pinnacle_api:
            api_key = os.getenv("THE_ODDS_API_KEY", "").strip()
            if not api_key:
                api_key = getpass.getpass("The Odds API key (input hidden): ").strip()
            budget = OddsRequestBudget(competitions=len(competitions))
            for competition in competitions:
                providers[competition] = TheOddsApiPinnacleProvider(
                    api_key, sport_key=SPORT_KEYS[competition], budget=budget,
                    snapshot_path=args.odds_snapshots,
                    max_alternate_requests=4 if len(competitions) == 2 else 10,
                    timeout_seconds=args.timeout_seconds,
                )

        with PolymarketPublicClient(timeout_seconds=args.timeout_seconds) as client:
            reports, section_warnings = build_market_reports(
                client, competitions=competitions, providers=providers, stake_usd=args.stake_usd)
            for warning in section_warnings:
                print(f"WARNING: {warning}", flush=True)
            for report in reports:
                print(render_terminal(report), flush=True)

            book_count = sum(
                len(fixture.outcomes)
                + (len(fixture.handicap.outcomes) if fixture.handicap else 0)
                for report in reports for fixture in report.fixtures
            )
            healthy_count = sum(
                outcome.error is None
                for report in reports for fixture in report.fixtures
                for outcome in fixture.outcomes
            ) + sum(
                outcome.error is None
                for report in reports for fixture in report.fixtures
                if fixture.handicap is not None
                for outcome in fixture.handicap.outcomes
            )
            exit_code = 0 if any(report.fixtures for report in reports) and not section_warnings and healthy_count == book_count else 2
            raise SystemExit(exit_code)
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0) from None
    except BotError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
