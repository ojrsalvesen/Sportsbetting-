from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from polymarket_bot.analytics_models import BetTrade, ClosedPosition, MarketEvent
from polymarket_bot.history import (
    AnalyticsStore,
    PolymarketHistoryClient,
    SUPPORTED_COMPETITIONS,
    is_season_competition_slug,
    is_season_epl_fixture_slug,
    is_season_epl_slug,
    validate_profile_address,
)


USER = "0x1111111111111111111111111111111111111111"


def trade_payload() -> dict[str, object]:
    return {
        "proxyWallet": USER,
        "timestamp": 1787600000,
        "conditionId": "condition-home",
        "type": "TRADE",
        "size": 10,
        "usdcSize": 5,
        "transactionHash": "0xtrade",
        "price": 0.5,
        "asset": "yes-token",
        "side": "BUY",
        "outcomeIndex": 0,
        "title": "Will Fulham win?",
        "eventSlug": "epl-ful-che-2026-08-24",
        "outcome": "Yes",
    }


def event_payload() -> dict[str, object]:
    def market(role: str, group: str) -> dict[str, object]:
        return {
            "conditionId": f"condition-{role}",
            "sportsMarketType": "moneyline",
            "groupItemTitle": group,
            "question": "Will this finish as a draw?" if role == "draw" else f"Will {group} win?",
        }

    return {
        "title": "Fulham FC vs. Chelsea FC",
        "eventStartTime": "2026-08-24T19:00:00Z",
        "markets": [
            market("away", "Chelsea FC"),
            market("draw", "Draw (Fulham FC vs. Chelsea FC)"),
            market("home", "Fulham FC"),
        ],
    }


class PublicHistoryTests(unittest.TestCase):
    def test_undated_epl_markets_use_record_time_for_season_assignment(self) -> None:
        undated = "epl-winner-2026-27"
        placed_in_2026 = 1787600000
        self.assertTrue(is_season_epl_slug(undated, 2026, timestamp=placed_in_2026))
        self.assertFalse(is_season_epl_slug(undated, 2025, timestamp=placed_in_2026))
        self.assertFalse(is_season_epl_fixture_slug(undated, 2026))

    def test_champions_league_slugs_are_included_in_the_shared_season_scope(self) -> None:
        self.assertTrue(is_season_competition_slug("ucl-ars-real-2026-09-15", 2026))
        self.assertTrue(is_season_competition_slug("champions-league-winner", 2026, timestamp=1787600000))
        self.assertTrue(is_season_competition_slug("uefa-champions-league-winner", 2026, timestamp=1787600000))
        self.assertFalse(is_season_competition_slug("ucl-ars-real-2025-09-15", 2026))
        self.assertFalse(is_season_competition_slug("politics-example", 2026))
        self.assertTrue(
            is_season_epl_fixture_slug("epl-ful-che-2026-08-24", 2026)
        )
        self.assertTrue(
            is_season_epl_fixture_slug(
                "epl-ful-che-2026-08-24-more-markets", 2026
            )
        )

    def test_fetches_only_epl_trades_and_maps_event_roles(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/activity":
                self.assertEqual(request.url.params["user"], USER)
                self.assertEqual(request.url.params["type"], "TRADE")
                other = {**trade_payload(), "eventSlug": "politics-example"}
                return httpx.Response(200, json=[trade_payload(), other])
            if request.url.path.endswith("/events/slug/epl-ful-che-2026-08-24"):
                return httpx.Response(200, json=event_payload())
            raise AssertionError(request.url)

        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            client = PolymarketHistoryClient(client=http_client)
            trades = client.trades_for_season(USER, 2026)
            event = client.event("epl-ful-che-2026-08-24")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].usdc_size, 5)
        self.assertEqual(
            event.condition_roles,
            {"condition-away": "away", "condition-draw": "draw", "condition-home": "home"},
        )

    def test_rejects_private_key_shaped_or_invalid_input(self) -> None:
        with self.assertRaisesRegex(Exception, "profile address"):
            validate_profile_address("not-an-address")

    def test_more_markets_event_keeps_fixture_but_has_no_moneyline_roles(self) -> None:
        payload = {
            "title": "Fulham FC vs. Chelsea FC - More Markets",
            "endDate": "2026-08-24T19:00:00Z",
            "markets": [
                {
                    "conditionId": "totals-condition",
                    "sportsMarketType": "totals",
                    "groupItemTitle": "O/U 2.5",
                    "question": "Fulham vs Chelsea: O/U 2.5",
                    "outcomes": '["Over", "Under"]',
                    "outcomePrices": '["0", "1"]',
                    "closed": True,
                    "umaResolutionStatus": "resolved",
                }
            ],
        }

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            event = PolymarketHistoryClient(client=http_client).event(
                "epl-ful-che-2026-08-24-more-markets"
            )
        self.assertEqual(event.title, "Fulham FC vs. Chelsea FC")
        self.assertEqual(event.condition_roles, {})
        self.assertEqual(event.winning_outcomes, {"totals-condition": "Under"})


class StoreTests(unittest.TestCase):
    def test_event_winning_outcomes_round_trip(self) -> None:
        event = MarketEvent(
            event_slug="epl-ful-che-2026-08-24-more-markets",
            title="Fulham vs Chelsea",
            kickoff=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
            home_team="Fulham",
            away_team="Chelsea",
            condition_roles={},
            winning_outcomes={"condition-btts": "No"},
        )
        with tempfile.TemporaryDirectory() as directory:
            with AnalyticsStore(Path(directory) / "history.sqlite3") as store:
                store.upsert_event(event)
                restored = store.events()[event.event_slug]

        self.assertEqual(restored.winning_outcomes, {"condition-btts": "No"})

    def test_stored_trade_query_excludes_non_football_and_wrong_season_slugs(self) -> None:
        epl = PolymarketHistoryClient(
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json=[trade_payload()])
                )
            )
        ).trades_for_season(USER, 2026)[0]
        ucl = BetTrade.from_api(
            {**trade_payload(), "eventSlug": "ucl-ars-real-2026-09-15", "asset": "ucl"},
            user=USER,
        )
        non_football = BetTrade.from_api(
            {**trade_payload(), "eventSlug": "politics-example", "asset": "politics"},
            user=USER,
        )
        wrong_fixture_season = BetTrade.from_api(
            {
                **trade_payload(),
                "eventSlug": "epl-ful-che-2025-08-24",
                "asset": "old-fixture",
            },
            user=USER,
        )
        with tempfile.TemporaryDirectory() as directory:
            with AnalyticsStore(Path(directory) / "history.sqlite3") as store:
                store.upsert_trades([epl, ucl, non_football, wrong_fixture_season])
                selected = store.trades(
                    USER, 2026, competitions=SUPPORTED_COMPETITIONS
                )

        self.assertEqual(
            [item.event_slug for item in selected], [epl.event_slug, ucl.event_slug]
        )

    def test_undated_closed_position_is_counted_only_in_its_close_season(self) -> None:
        position = ClosedPosition.from_api(
            {
                "asset": "season-winner-token",
                "conditionId": "season-winner",
                "eventSlug": "epl-winner-2026-27",
                "outcome": "Yes",
                "avgPrice": 0.1,
                "totalBought": 10,
                "realizedPnl": 2,
                "timestamp": 1787600000,
            },
            user=USER,
        )
        with tempfile.TemporaryDirectory() as directory:
            with AnalyticsStore(Path(directory) / "history.sqlite3") as store:
                store.upsert_closed_positions([position])
                self.assertEqual(store.realized_pnl(USER, 2026), (1, 2))
                self.assertEqual(store.realized_pnl(USER, 2025), (0, 0))

    def test_trade_sync_is_idempotent_and_database_can_be_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            trade = PolymarketHistoryClient(
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json=[trade_payload()])
                    )
                )
            ).trades_for_season(USER, 2026)[0]
            with AnalyticsStore(path) as store:
                self.assertEqual(store.upsert_trades([trade]), (1, 1))
                self.assertEqual(store.upsert_trades([trade]), (0, 1))
            with AnalyticsStore(path) as store:
                self.assertEqual(len(store.trades(USER, 2026)), 1)
                self.assertEqual(store.known_users(), (USER,))

    def test_profile_is_remembered_even_before_it_has_trades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with AnalyticsStore(Path(directory) / "history.sqlite3") as store:
                store.remember_user(USER)
                self.assertEqual(store.known_users(), (USER,))


if __name__ == "__main__":
    unittest.main()
