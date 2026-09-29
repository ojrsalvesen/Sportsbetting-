from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from polymarket_bot.market_data import PolymarketPublicClient
from polymarket_bot.reporting import discover_fixtures
from polymarket_bot.history import PolymarketHistoryClient
from polymarket_bot.xg import UnderstatXgClient


@unittest.skipUnless(
    os.getenv("PMR_RUN_NETWORK_TESTS") == "1",
    "set PMR_RUN_NETWORK_TESTS=1 for read-only public API tests",
)
class PublicNetworkContractTests(unittest.TestCase):
    def test_epl_discovery_and_batch_book_shapes(self) -> None:
        with PolymarketPublicClient(timeout_seconds=20) as client:
            series_id, events = client.list_epl_events()
            fixtures, warnings = discover_fixtures(events)
            self.assertTrue(series_id)
            self.assertFalse(warnings)
            self.assertTrue(fixtures, "No upcoming EPL fixtures available for contract test")
            token_ids = [
                token_id
                for outcome in fixtures[0].outcomes
                for _, token_id in outcome.token_ids()
            ]
            books = client.get_order_books(token_ids)
        self.assertEqual(set(books), set(token_ids))
        self.assertTrue(all(book.tick_size > 0 for book in books.values()))

    def test_analytics_event_and_xg_shapes(self) -> None:
        with PolymarketPublicClient(timeout_seconds=20) as market_client:
            _, raw_events = market_client.list_epl_events()
            fixtures, _ = discover_fixtures(raw_events)
        self.assertTrue(fixtures, "No upcoming EPL fixture available for analytics contract")
        with PolymarketHistoryClient(timeout_seconds=20) as history_client:
            event = history_client.event(fixtures[0].slug)
        with UnderstatXgClient(timeout_seconds=20) as xg_client:
            now = datetime.now(timezone.utc)
            season = now.year if now.month >= 7 else now.year - 1
            # The previous completed season also works during the summer break.
            matches = xg_client.matches(season - 1)
        self.assertEqual(set(event.condition_roles.values()), {"home", "draw", "away"})
        self.assertTrue(matches, "No completed EPL xG matches returned")
        self.assertTrue(all(match.home_xg >= 0 and match.away_xg >= 0 for match in matches))


if __name__ == "__main__":
    unittest.main()
