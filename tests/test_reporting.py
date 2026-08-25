from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from polymarket_bot.errors import ConfigError
from polymarket_bot.models import OrderBook
from polymarket_bot.reporting import (
    FREE_TIER_PINNACLE_REFRESH_SECONDS,
    OddsQuote,
    TheOddsApiPinnacleProvider,
    build_premier_league_report,
    calculate_book_metrics,
    discover_fixtures,
    moneyline_fixture_from_event,
    normalize_team,
    render_terminal,
)


NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)


def raw_market(role: str, label: str, token: str) -> dict[str, object]:
    question = (
        "Will Fulham FC vs. Chelsea FC end in a draw?"
        if role == "draw"
        else f"Will {label} win on 2026-08-24?"
    )
    return {
        "id": role,
        "conditionId": f"condition-{role}",
        "slug": f"fixture-{role}",
        "question": question,
        "description": "Full-time result",
        "resolutionSource": "Premier League",
        "groupItemTitle": (
            "Draw (Fulham FC vs. Chelsea FC)" if role == "draw" else label
        ),
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{token}", "no-{token}"]',
        "sportsMarketType": "moneyline",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "restricted": False,
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "negRisk": True,
        "endDate": "2026-08-24T19:00:00Z",
    }


def raw_event() -> dict[str, object]:
    return {
        "id": "event-1",
        "title": "Fulham FC vs. Chelsea FC",
        "slug": "epl-ful-che-2026-08-24",
        "endDate": "2026-08-24T19:00:00Z",
        # Deliberately not in home/draw/away order.
        "markets": [
            raw_market("away", "Chelsea FC", "away-token"),
            raw_market("draw", "Draw", "draw-token"),
            raw_market("home", "Fulham FC", "home-token"),
        ],
    }


class FixtureDiscoveryTests(unittest.TestCase):
    def test_moneyline_is_mapped_and_ordered_home_draw_away(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        self.assertIsNotNone(fixture)
        assert fixture is not None
        self.assertEqual([outcome.role for outcome in fixture.outcomes], ["home", "draw", "away"])
        self.assertEqual(
            [outcome.market.token_id for outcome in fixture.outcomes],
            ["home-token", "draw-token", "away-token"],
        )
        self.assertEqual(
            [outcome.no_token_id for outcome in fixture.outcomes],
            ["no-home-token", "no-draw-token", "no-away-token"],
        )

    def test_report_fetches_yes_and_no_books_for_every_outcome(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None

        class FakeClient:
            def list_epl_events(self) -> tuple[str, list[dict[str, object]]]:
                return "10188", [raw_event()]

            def get_order_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
                condition_by_token = {
                    token_id: outcome.market.condition_id
                    for outcome in fixture.outcomes
                    for _, token_id in outcome.token_ids()
                }
                return {
                    token_id: OrderBook.from_api(
                        {
                            "asset_id": token_id,
                            "market": condition_by_token[token_id],
                            "timestamp": str(int(NOW.timestamp() * 1000)),
                            "tick_size": "0.01",
                            "min_order_size": "5",
                            "neg_risk": True,
                            "bids": [{"price": "0.49", "size": "10"}],
                            "asks": [{"price": "0.51", "size": "10"}],
                        },
                    )
                    for token_id in token_ids
                }

        report = build_premier_league_report(FakeClient(), now=NOW)  # type: ignore[arg-type]
        token_reports = report.fixtures[0].outcomes
        self.assertEqual(len(token_reports), 6)
        self.assertEqual(
            [(item.outcome.role, item.token_side) for item in token_reports],
            [
                ("home", "YES"),
                ("home", "NO"),
                ("draw", "YES"),
                ("draw", "NO"),
                ("away", "YES"),
                ("away", "NO"),
            ],
        )
        terminal = render_terminal(report)
        self.assertIn("Fulham FC", terminal)
        self.assertIn("YES", terminal)
        self.assertIn("NO", terminal)

    def test_non_moneyline_and_started_events_are_excluded(self) -> None:
        event = raw_event()
        event["endDate"] = "2026-08-23T19:00:00Z"
        fixture = moneyline_fixture_from_event(event, now=NOW)
        self.assertIsNone(fixture)
        fixtures, warnings = discover_fixtures([event], now=NOW)
        self.assertEqual(fixtures, [])
        self.assertEqual(warnings, [])


class OrderBookMetricTests(unittest.TestCase):
    def test_depth_spread_and_five_dollar_vwap(self) -> None:
        book = OrderBook.from_api(
            {
                "asset_id": "home-token",
                "market": "condition-home",
                "timestamp": str(int(NOW.timestamp() * 1000)),
                "tick_size": "0.01",
                "min_order_size": "5",
                "neg_risk": True,
                "bids": [
                    {"price": "0.48", "size": "20"},
                    {"price": "0.49", "size": "10"},
                    {"price": "0.40", "size": "100"},
                ],
                "asks": [
                    {"price": "0.60", "size": "100"},
                    {"price": "0.52", "size": "10"},
                    {"price": "0.51", "size": "4"},
                ],
            },
        )
        metrics = calculate_book_metrics(book, target_spend_usd=Decimal("5"))
        self.assertEqual(metrics.best_bid, Decimal("0.49"))
        self.assertEqual(metrics.best_ask, Decimal("0.51"))
        self.assertEqual(metrics.spread, Decimal("0.02"))
        self.assertEqual(metrics.bid_depth_usd["1c"], Decimal("14.50"))
        self.assertEqual(metrics.ask_depth_usd["1c"], Decimal("7.24"))
        self.assertEqual(metrics.fillable_spend_usd, Decimal("5.00"))
        self.assertAlmostEqual(float(metrics.buy_vwap or 0), 0.515875, places=5)


class OddsTests(unittest.TestCase):
    def test_no_vig_probabilities_sum_to_one(self) -> None:
        quote = OddsQuote(
            home_team="Fulham",
            away_team="Chelsea",
            starts_at=NOW,
            captured_at=NOW,
            home_odds=Decimal("4.0"),
            draw_odds=Decimal("4.0"),
            away_odds=Decimal("2.0"),
        )
        total = sum((quote.fair_probability(role) for role in ("home", "draw", "away")), Decimal("0"))
        self.assertEqual(total, Decimal("1"))
        self.assertEqual(quote.fair_probability("away"), Decimal("0.5"))

    def test_team_names_are_normalized_for_matching(self) -> None:
        self.assertEqual(normalize_team("AFC Bournemouth"), "bournemouth")
        self.assertEqual(normalize_team("Man Utd"), "manchester united")

    def test_the_odds_api_parses_pinnacle_and_reports_quota(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.url.params["bookmakers"], "pinnacle")
            self.assertEqual(request.url.params["markets"], "h2h")
            return httpx.Response(
                200,
                headers={
                    "x-requests-remaining": "499",
                    "x-requests-used": "1",
                    "x-requests-last": "1",
                },
                json=[
                    {
                        "commence_time": "2026-08-24T19:00:00Z",
                        "home_team": "Fulham",
                        "away_team": "Chelsea",
                        "bookmakers": [
                            {
                                "key": "pinnacle",
                                "last_update": "2026-08-24T12:00:00Z",
                                "markets": [
                                    {
                                        "key": "h2h",
                                        "last_update": "2026-08-24T12:00:00Z",
                                        "outcomes": [
                                            {"name": "Chelsea", "price": 1.90},
                                            {"name": "Draw", "price": 3.50},
                                            {"name": "Fulham", "price": 4.00},
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = TheOddsApiPinnacleProvider("top-secret", client=client)
            self.assertTrue(provider.refresh_if_due(force=True))
            self.assertFalse(provider.refresh_if_due())
            quote = provider.quote_for(fixture)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.home_odds, Decimal("4.0"))
        self.assertEqual(provider.credits_remaining, 499)
        self.assertIn("499 credits remaining", provider.description)
        self.assertNotIn("top-secret", repr(provider))
        self.assertEqual(calls, 1)

    def test_free_only_api_refresh_rejects_faster_polling(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Free-only"):
            TheOddsApiPinnacleProvider(
                "key",
                refresh_seconds=FREE_TIER_PINNACLE_REFRESH_SECONDS - 1,
            )
if __name__ == "__main__":
    unittest.main()
