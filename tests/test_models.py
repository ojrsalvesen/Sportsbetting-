from __future__ import annotations

import unittest

from polymarket_bot.models import OrderBook


class OrderBookTests(unittest.TestCase):
    def test_best_prices_do_not_depend_on_api_sort_order(self) -> None:
        book = OrderBook.from_api(
            {
                "asset_id": "token",
                "market": "condition",
                "bids": [
                    {"price": "0.10", "size": "2"},
                    {"price": "0.40", "size": "1"},
                ],
                "asks": [
                    {"price": "0.90", "size": "2"},
                    {"price": "0.51", "size": "1"},
                ],
                "tick_size": "0.01",
                "min_order_size": "5",
                "neg_risk": False,
            },
        )
        self.assertEqual(str(book.best_bid), "0.40")
        self.assertEqual(str(book.best_ask), "0.51")


if __name__ == "__main__":
    unittest.main()
