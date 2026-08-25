from __future__ import annotations

import unittest

import httpx

class BatchBookTests(unittest.TestCase):
    def test_batch_books_are_keyed_by_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/books")
            return httpx.Response(
                200,
                json=[
                    {
                        "asset_id": "token-1",
                        "market": "condition-1",
                        "timestamp": "1000",
                        "tick_size": "0.01",
                        "min_order_size": "5",
                        "neg_risk": False,
                        "bids": [],
                        "asks": [],
                    }
                ],
            )

        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport) as http_client:
            from polymarket_bot.market_data import PolymarketPublicClient

            client = PolymarketPublicClient(client=http_client)
            books = client.get_order_books(["token-1"])
        self.assertEqual(books["token-1"].condition_id, "condition-1")


if __name__ == "__main__":
    unittest.main()
