from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from polymarket_bot.analytics_models import BetTrade, ClosedPosition, MarketEvent, MatchXg
from polymarket_bot.history import AnalyticsStore
from polymarket_bot.notebook_data import load_notebook_data


USER = "0x1111111111111111111111111111111111111111"
OTHER_USER = "0x2222222222222222222222222222222222222222"


def trade(user: str = USER) -> BetTrade:
    return BetTrade.from_api(
        {
            "timestamp": 1787600000,
            "conditionId": "condition-home",
            "size": 10,
            "usdcSize": 5,
            "transactionHash": f"0x{user[-4:]}",
            "price": 0.5,
            "asset": "yes-token",
            "side": "BUY",
            "outcomeIndex": 0,
            "title": "Will Fulham win?",
            "eventSlug": "epl-ful-che-2026-08-24",
            "outcome": "Yes",
        },
        user=user,
    )


def closed_position(user: str, slug: str) -> ClosedPosition:
    return ClosedPosition.from_api(
        {
            "asset": f"asset-{user[-4:]}-{slug}",
            "conditionId": "condition-home",
            "eventSlug": slug,
            "outcome": "Yes",
            "avgPrice": 0.5,
            "totalBought": 10,
            "realizedPnl": 2,
            "timestamp": 1787700000,
        },
        user=user,
    )


class NotebookDataTests(unittest.TestCase):
    def test_loads_pandas_frames_for_only_the_selected_profile_and_season(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "history.sqlite3"
            with AnalyticsStore(database) as store:
                store.remember_user(USER)
                store.upsert_trades([trade(), trade(OTHER_USER)])
                store.upsert_closed_positions(
                    [
                        closed_position(USER, "epl-ful-che-2026-08-24"),
                        closed_position(USER, "epl-ful-che-2025-08-24"),
                        closed_position(OTHER_USER, "epl-ful-che-2026-08-24"),
                    ]
                )
                store.upsert_event(
                    MarketEvent(
                        event_slug="epl-ful-che-2026-08-24",
                        title="Fulham vs Chelsea",
                        kickoff=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
                        home_team="Fulham",
                        away_team="Chelsea",
                        condition_roles={"condition-home": "home"},
                    )
                )
                store.upsert_xg_matches(
                    [
                        MatchXg(
                            match_id="match-2026",
                            season=2026,
                            kickoff=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
                            home_team="Fulham",
                            away_team="Chelsea",
                            home_goals=1,
                            away_goals=0,
                            home_xg=Decimal("1.4"),
                            away_xg=Decimal("0.7"),
                        ),
                        MatchXg(
                            match_id="match-2025",
                            season=2025,
                            kickoff=datetime(2025, 8, 24, 19, tzinfo=timezone.utc),
                            home_team="Fulham",
                            away_team="Chelsea",
                            home_goals=0,
                            away_goals=1,
                            home_xg=Decimal("0.6"),
                            away_xg=Decimal("1.2"),
                        ),
                    ]
                )

            frames = load_notebook_data(database, season=2026, user=USER)

            self.assertEqual(frames["profile_address"], USER)
            self.assertEqual(len(frames["bets"]), 1)
            self.assertEqual(len(frames["trades"]), 1)
            self.assertEqual(len(frames["closed_positions"]), 1)
            self.assertEqual(len(frames["xg_matches"]), 2)
            self.assertEqual(frames["bets"].iloc[0]["fixture"], "Fulham vs Chelsea")
            self.assertEqual(frames["bets"].iloc[0]["asset"], "yes-token")
            self.assertNotIn("model_edge", frames["bets"].columns)
            self.assertEqual(frames["trades"].iloc[0]["user"], USER)
            self.assertEqual(
                frames["closed_positions"].iloc[0]["event_slug"],
                "epl-ful-che-2026-08-24",
            )
            self.assertTrue(str(frames["trades"]["price"].dtype).startswith("float"))


if __name__ == "__main__":
    unittest.main()
