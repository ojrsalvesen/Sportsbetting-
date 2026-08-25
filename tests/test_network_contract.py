from __future__ import annotations

import os
import unittest

from polymarket_bot.market_data import PolymarketPublicClient
from polymarket_bot.reporting import discover_fixtures


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


if __name__ == "__main__":
    unittest.main()
