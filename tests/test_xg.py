from __future__ import annotations

import unittest
from decimal import Decimal

import httpx

from polymarket_bot.xg import UnderstatXgClient


class UnderstatTests(unittest.TestCase):
    def test_parses_only_completed_matches_and_sends_xhr_headers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/getLeagueData/EPL/2026")
            self.assertEqual(request.headers["x-requested-with"], "XMLHttpRequest")
            return httpx.Response(
                200,
                json={
                    "dates": [
                        {
                            "id": "31180",
                            "isResult": True,
                            "h": {"title": "Arsenal"},
                            "a": {"title": "Coventry"},
                            "goals": {"h": "3", "a": "0"},
                            "xG": {"h": "1.85424", "a": "0.558336"},
                            "datetime": "2026-08-21 19:00:00",
                        },
                        {"id": "future", "isResult": False},
                    ]
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            matches = UnderstatXgClient(client=http_client).matches(2026)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].home_team, "Arsenal")
        self.assertEqual(matches[0].home_xg, Decimal("1.85424"))


if __name__ == "__main__":
    unittest.main()
