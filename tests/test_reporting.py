from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from polymarket_bot.errors import ConfigError
from polymarket_bot.models import OrderBook
from polymarket_bot.reporting import (
    FREE_TIER_PINNACLE_REFRESH_SECONDS,
    HandicapQuote,
    OddsQuote,
    TheOddsApiPinnacleProvider,
    build_premier_league_report,
    calculate_book_metrics,
    discover_fixtures,
    discover_popular_handicaps,
    moneyline_fixture_from_event,
    normalize_team,
    render_terminal,
    select_upcoming_week,
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


def raw_handicap_event() -> dict[str, object]:
    def spread(
        market_id: str,
        first_team: str,
        second_team: str,
        *,
        volume: str,
        liquidity: str,
    ) -> dict[str, object]:
        return {
            "id": market_id,
            "conditionId": f"condition-{market_id}",
            "question": f"Spread: {first_team} (-1.5)",
            "sportsMarketType": "spreads",
            "line": "-1.5",
            "outcomes": f'["{first_team}", "{second_team}"]',
            "clobTokenIds": f'["{market_id}-first", "{market_id}-second"]',
            "volume": volume,
            "liquidity": liquidity,
        }

    return {
        "id": "event-1-more",
        "title": "Fulham FC vs. Chelsea FC - More Markets",
        "slug": "epl-ful-che-2026-08-24-more-markets",
        "endDate": "2026-08-24T19:00:00Z",
        "markets": [
            spread(
                "home-spread", "Fulham FC", "Chelsea FC",
                volume="10", liquidity="500",
            ),
            spread(
                "away-spread", "Chelsea FC", "Fulham FC",
                volume="25", liquidity="400",
            ),
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

    def test_pinnacle_gap_uses_full_stake_vwap_instead_of_best_ask(self) -> None:
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
                            "asks": [
                                {"price": "0.51", "size": "4"},
                                {"price": "0.52", "size": "10"},
                            ],
                        }
                    )
                    for token_id in token_ids
                }

        class FakeOddsProvider:
            description = "test Pinnacle quote"

            def quote_for(self, _: object) -> OddsQuote:
                return OddsQuote(
                    home_team="Fulham FC",
                    away_team="Chelsea FC",
                    starts_at=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
                    captured_at=NOW,
                    home_odds=Decimal("2"),
                    draw_odds=Decimal("4"),
                    away_odds=Decimal("4"),
                )

        report = build_premier_league_report(
            FakeClient(),  # type: ignore[arg-type]
            odds_provider=FakeOddsProvider(),  # type: ignore[arg-type]
            now=NOW,
        )
        home_yes = report.fixtures[0].outcomes[0]
        self.assertAlmostEqual(float(home_yes.price_gap or 0), -0.015875, places=5)
        self.assertNotEqual(home_yes.price_gap, Decimal("0.5") - Decimal("0.51"))

    def test_non_moneyline_and_started_events_are_excluded(self) -> None:
        event = raw_event()
        event["endDate"] = "2026-08-23T19:00:00Z"
        fixture = moneyline_fixture_from_event(event, now=NOW)
        self.assertIsNone(fixture)
        fixtures, warnings = discover_fixtures([event], now=NOW)
        self.assertEqual(fixtures, [])
        self.assertEqual(warnings, [])

    def test_upcoming_week_includes_weekend_across_long_gap(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        same_round = replace(
            fixture,
            event_id="event-2",
            title="Second fixture",
            starts_at=NOW + timedelta(days=6),
        )
        following_round = replace(
            fixture,
            event_id="event-3",
            title="Following-round fixture",
            starts_at=same_round.starts_at + timedelta(days=6),
        )
        selected = select_upcoming_week([following_round, same_round, fixture], now=NOW)
        self.assertEqual([item.event_id for item in selected], ["event-1", "event-2"])

    def test_upcoming_week_boundaries_and_no_fixture_cap(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        fixtures = [replace(fixture, event_id=str(i), starts_at=NOW + timedelta(hours=i))
                    for i in range(-1, 13)]
        fixtures += [
            replace(fixture, event_id="cutoff", starts_at=NOW + timedelta(days=7)),
            replace(fixture, event_id="outside", starts_at=NOW + timedelta(days=7, seconds=1)),
        ]
        selected = select_upcoming_week(list(reversed(fixtures)), now=NOW)
        self.assertEqual([item.event_id for item in selected],
                         [str(i) for i in range(1, 13)] + ["cutoff"])
        self.assertEqual(select_upcoming_week([], now=NOW), [])
        self.assertEqual(select_upcoming_week([fixtures[-1]], now=NOW), [])

    def test_highest_volume_full_match_handicap_is_selected(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        selected, warnings = discover_popular_handicaps(
            [raw_handicap_event()], [fixture]
        )
        self.assertEqual(warnings, [])
        handicap = selected[fixture.event_id]
        self.assertEqual(handicap.title, "Spread: Chelsea FC (-1.5)")
        self.assertEqual(
            [(outcome.role, outcome.line) for outcome in handicap.outcomes],
            [("home", Decimal("1.5")), ("away", Decimal("-1.5"))],
        )

        preferred, _ = discover_popular_handicaps(
            [raw_handicap_event()],
            [fixture],
            preferred_lines={
                fixture.event_id: (Decimal("-1.5"), Decimal("1.5"))
            },
        )
        self.assertEqual(preferred[fixture.event_id].title, "Spread: Fulham FC (-1.5)")

        favorite_only, _ = discover_popular_handicaps(
            [raw_handicap_event()],
            [fixture],
            favorite_roles={fixture.event_id: "home"},
        )
        self.assertEqual(
            favorite_only[fixture.event_id].title, "Spread: Fulham FC (-1.5)"
        )

    def test_report_fetches_and_renders_selected_handicap_books(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        handicaps, _ = discover_popular_handicaps([raw_handicap_event()], [fixture])
        handicap = handicaps[fixture.event_id]
        requested: list[str] = []

        class FakeClient:
            def list_epl_events(self) -> tuple[str, list[dict[str, object]]]:
                return "10188", [raw_event(), raw_handicap_event()]

            def get_order_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
                requested.extend(token_ids)
                condition_by_token = {
                    token_id: outcome.market.condition_id
                    for outcome in fixture.outcomes
                    for _, token_id in outcome.token_ids()
                }
                condition_by_token.update(
                    {
                        outcome.market.token_id: outcome.market.condition_id
                        for outcome in handicap.outcomes
                    }
                )
                return {
                    token_id: OrderBook.from_api(
                        {
                            "asset_id": token_id,
                            "market": condition_by_token[token_id],
                            "tick_size": "0.01",
                            "min_order_size": "5",
                            "neg_risk": True,
                            "bids": [{"price": "0.49", "size": "20"}],
                            "asks": [{"price": "0.51", "size": "20"}],
                        }
                    )
                    for token_id in token_ids
                }

        class FakeOddsProvider:
            description = "test Pinnacle moneyline + handicap"

            def quote_for(self, _: object) -> OddsQuote:
                return OddsQuote(
                    home_team="Fulham FC",
                    away_team="Chelsea FC",
                    starts_at=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
                    captured_at=NOW,
                    home_odds=Decimal("4"),
                    draw_odds=Decimal("3.5"),
                    away_odds=Decimal("1.9"),
                    handicap=HandicapQuote(
                        home_line=Decimal("1.5"),
                        away_line=Decimal("-1.5"),
                        home_odds=Decimal("2"),
                        away_odds=Decimal("2"),
                        captured_at=NOW,
                    ),
                )

        report = build_premier_league_report(
            FakeClient(),  # type: ignore[arg-type]
            odds_provider=FakeOddsProvider(),  # type: ignore[arg-type]
            now=NOW,
        )
        self.assertEqual(len(requested), 8)
        self.assertIsNotNone(report.fixtures[0].handicap)
        assert report.fixtures[0].handicap is not None
        self.assertTrue(
            all(
                outcome.pinnacle_fair_probability == Decimal("0.5")
                for outcome in report.fixtures[0].handicap.outcomes
            )
        )
        terminal = render_terminal(report)
        self.assertIn("Favourite-team handicap", terminal)
        self.assertIn("Chelsea FC -1.5", terminal)


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

    def test_handicap_fair_probability_requires_the_exact_line(self) -> None:
        handicap = HandicapQuote(
            home_line=Decimal("1.5"),
            away_line=Decimal("-1.5"),
            home_odds=Decimal("2.0"),
            away_odds=Decimal("2.0"),
            captured_at=NOW,
        )
        self.assertEqual(handicap.fair_probability("away", Decimal("-1.5")), Decimal("0.5"))
        self.assertIsNone(handicap.fair_probability("away", Decimal("-2.5")))

    def test_the_odds_api_parses_pinnacle_and_reports_quota(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.url.params["bookmakers"], "pinnacle")
            self.assertEqual(request.url.params["markets"], "h2h,spreads")
            self.assertEqual(request.url.params["commenceTimeFrom"], "2026-08-24T19:00:00Z")
            self.assertEqual(request.url.params["commenceTimeTo"], "2026-08-24T19:00:00Z")
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
                                    },
                                    {
                                        "key": "spreads",
                                        "last_update": "2026-08-24T12:01:00Z",
                                        "outcomes": [
                                            {"name": "Chelsea", "price": 1.91, "point": -1.5},
                                            {"name": "Fulham", "price": 1.99, "point": 1.5},
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            provider = TheOddsApiPinnacleProvider("top-secret", client=client)
            provider.set_fixture_window([fixture])
            self.assertTrue(provider.refresh_if_due(force=True))
            self.assertFalse(provider.refresh_if_due())
            quote = provider.quote_for(fixture)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.home_odds, Decimal("4.0"))
        self.assertEqual(
            quote.handicap,
            HandicapQuote(
                home_line=Decimal("1.5"),
                away_line=Decimal("-1.5"),
                home_odds=Decimal("1.99"),
                away_odds=Decimal("1.91"),
                captured_at=datetime(2026, 8, 24, 12, 1, tzinfo=timezone.utc),
            ),
        )
        self.assertEqual(provider.credits_remaining, 499)
        self.assertIn("499 credits remaining", provider.description)
        self.assertNotIn("top-secret", repr(provider))
        self.assertEqual(calls, 1)

    def test_exact_alternate_handicap_is_loaded_when_featured_line_is_missing(self) -> None:
        fixture = moneyline_fixture_from_event(raw_event(), now=NOW)
        assert fixture is not None
        selected, _ = discover_popular_handicaps(
            [raw_handicap_event()],
            [fixture],
            favorite_roles={fixture.event_id: "away"},
        )
        requested_markets: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            market_key = request.url.params["markets"]
            requested_markets.append(market_key)
            headers = {
                "x-requests-remaining": "498",
                "x-requests-used": "2",
                "x-requests-last": "1",
            }
            if market_key == "alternate_spreads":
                self.assertIn("/events/odds-event-1/odds", request.url.path)
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "id": "odds-event-1",
                        "bookmakers": [
                            {
                                "key": "pinnacle",
                                "markets": [
                                    {
                                        "key": "alternate_spreads",
                                        "last_update": "2026-08-24T12:02:00Z",
                                        "outcomes": [
                                            {"name": "Chelsea", "price": 2.10, "point": -1.5},
                                            {"name": "Fulham", "price": 1.80, "point": 1.5},
                                            {"name": "Chelsea", "price": 3.50, "point": -2.5},
                                            {"name": "Fulham", "price": 1.30, "point": 2.5},
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "id": "odds-event-1",
                        "commence_time": "2026-08-24T19:00:00Z",
                        "home_team": "Fulham",
                        "away_team": "Chelsea",
                        "bookmakers": [
                            {
                                "key": "pinnacle",
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
            provider.set_fixture_window([fixture])
            provider.refresh_if_due(force=True)
            warnings = provider.add_exact_alternate_handicaps([fixture], selected)
            quote = provider.quote_for(fixture)

        self.assertEqual(warnings, [])
        self.assertEqual(requested_markets, ["h2h,spreads", "alternate_spreads"])
        self.assertIsNotNone(quote)
        assert quote is not None and quote.handicap is not None
        self.assertEqual(quote.handicap.home_line, Decimal("1.5"))
        self.assertEqual(quote.handicap.away_line, Decimal("-1.5"))
        self.assertIsNotNone(
            quote.handicap.fair_probability("away", Decimal("-1.5"))
        )

    def test_free_only_api_refresh_rejects_faster_polling(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Free-only"):
            TheOddsApiPinnacleProvider(
                "key",
                refresh_seconds=FREE_TIER_PINNACLE_REFRESH_SECONDS - 1,
            )
if __name__ == "__main__":
    unittest.main()
