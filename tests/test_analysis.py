from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.analysis import analyze_bets
from polymarket_bot.analytics_models import BetTrade, MarketEvent, MatchXg


def match(
    match_id: str,
    kickoff: str,
    home: str,
    away: str,
    goals: tuple[int, int],
    xg: tuple[str, str],
) -> MatchXg:
    return MatchXg(
        match_id=match_id,
        season=2026,
        kickoff=datetime.fromisoformat(kickoff),
        home_team=home,
        away_team=away,
        home_goals=goals[0],
        away_goals=goals[1],
        home_xg=Decimal(xg[0]),
        away_xg=Decimal(xg[1]),
    )


class AnalysisTests(unittest.TestCase):
    def test_buy_fill_gets_pre_match_form_and_uses_recorded_cost_for_hold_pnl(self) -> None:
        kickoff = datetime(2026, 8, 24, 19, tzinfo=timezone.utc)
        event = MarketEvent(
            event_slug="epl-ful-che-2026-08-24",
            title="Fulham FC vs. Chelsea FC",
            kickoff=kickoff,
            home_team="Fulham FC",
            away_team="Chelsea FC",
            condition_roles={"condition-home": "home"},
        )
        trade = BetTrade(
            id="trade",
            user="0x" + "1" * 40,
            timestamp=int(datetime(2026, 8, 24, 10, tzinfo=timezone.utc).timestamp()),
            condition_id="condition-home",
            asset="yes-token",
            side="BUY",
            size=Decimal("10"),
            usdc_size=Decimal("5.11"),
            price=Decimal("0.5"),
            title="Will Fulham win?",
            event_slug=event.event_slug,
            outcome="Yes",
            outcome_index=0,
            transaction_hash="0xtrade",
            raw_json="{}",
        )
        matches = [
            match("old-home", "2026-08-15T14:00:00+00:00", "Fulham", "Everton", (1, 0), ("1.4", "0.7")),
            match("old-away", "2026-08-16T14:00:00+00:00", "Chelsea", "Arsenal", (2, 1), ("1.8", "1.1")),
            match("target", "2026-08-24T19:00:00+00:00", "Fulham", "Chelsea", (2, 0), ("1.6", "0.9")),
        ]
        result = analyze_bets([trade], {event.event_slug: event}, matches)[0]
        self.assertEqual(result.result, "WIN")
        self.assertEqual(result.hold_pnl, Decimal("4.89"))
        self.assertEqual(result.home_form.matches if result.home_form else None, 1)


if __name__ == "__main__":
    unittest.main()
