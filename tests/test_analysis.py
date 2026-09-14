from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.analysis import analyze_bets, xg_matches_for_events
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
    def test_buy_fill_matches_fixture_xg_and_uses_recorded_cost_for_hold_pnl(self) -> None:
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
        self.assertEqual(result.match.match_id if result.match else None, "target")
        self.assertEqual(
            (result.direction_team, result.direction_opponent, result.direction_is_home),
            ("Fulham FC", "Chelsea FC", True),
        )

        no_result = analyze_bets(
            [
                replace(
                    trade,
                    id="trade-no",
                    asset="no-token",
                    outcome="No",
                    outcome_index=1,
                )
            ],
            {event.event_slug: event},
            matches,
        )[0]
        self.assertEqual(no_result.result, "LOSS")
        self.assertEqual(
            (
                no_result.direction_team,
                no_result.direction_opponent,
                no_result.direction_is_home,
            ),
            ("Chelsea FC", "Fulham FC", False),
        )

    def test_xg_scope_contains_only_fixtures_represented_by_bets(self) -> None:
        event = MarketEvent(
            event_slug="epl-ful-che-2026-08-24",
            title="Fulham vs Chelsea",
            kickoff=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
            home_team="Fulham",
            away_team="Chelsea",
            condition_roles={},
        )
        matches = [
            match("target", "2026-08-24T19:00:00+00:00", "Fulham", "Chelsea", (1, 0), ("1.4", "0.7")),
            match("other", "2026-08-25T19:00:00+00:00", "Arsenal", "Leeds", (2, 0), ("2.1", "0.4")),
        ]

        scoped = xg_matches_for_events([event], matches)

        self.assertEqual([item.match_id for item in scoped], ["target"])

    def test_official_market_winner_settles_btts_without_xg(self) -> None:
        event = MarketEvent(
            event_slug="epl-ful-che-2026-08-24-more-markets",
            title="Fulham vs Chelsea",
            kickoff=datetime(2026, 8, 24, 19, tzinfo=timezone.utc),
            home_team="Fulham",
            away_team="Chelsea",
            condition_roles={},
            winning_outcomes={"condition-btts": "No"},
        )
        trade = BetTrade(
            id="btts-trade",
            user="0x" + "1" * 40,
            timestamp=int(datetime(2026, 8, 24, 10, tzinfo=timezone.utc).timestamp()),
            condition_id="condition-btts",
            asset="btts-no-token",
            side="BUY",
            size=Decimal("10"),
            usdc_size=Decimal("4"),
            price=Decimal("0.4"),
            title="Fulham vs Chelsea: Both Teams to Score",
            event_slug=event.event_slug,
            outcome="No",
            outcome_index=1,
            transaction_hash="0xbtts",
            raw_json="{}",
        )

        result = analyze_bets([trade], {event.event_slug: event}, [])[0]

        self.assertEqual(result.market_type, "Both teams to score")
        self.assertEqual(result.result, "WIN")
        self.assertEqual(result.resolution_source, "Polymarket")
        self.assertEqual(result.hold_pnl, Decimal("6"))
        self.assertIsNone(result.direction_team)

    def test_score_fallback_settles_btts_and_half_goal_spread(self) -> None:
        event = MarketEvent(
            event_slug="epl-mun-ips-2026-08-30-more-markets",
            title="Manchester United vs Ipswich Town",
            kickoff=datetime(2026, 8, 30, 15, tzinfo=timezone.utc),
            home_team="Manchester United",
            away_team="Ipswich Town",
            condition_roles={},
        )
        common = {
            "user": "0x" + "1" * 40,
            "timestamp": int(datetime(2026, 8, 29, 10, tzinfo=timezone.utc).timestamp()),
            "side": "BUY",
            "size": Decimal("10"),
            "usdc_size": Decimal("4"),
            "price": Decimal("0.4"),
            "event_slug": event.event_slug,
            "outcome_index": 0,
            "transaction_hash": "0xtest",
            "raw_json": "{}",
        }
        spread = BetTrade(
            id="spread",
            condition_id="condition-spread",
            asset="spread-token",
            title="Spread: Manchester United (-1.5)",
            outcome="Manchester United",
            **common,
        )
        btts = BetTrade(
            id="btts",
            condition_id="condition-btts",
            asset="btts-token",
            title="Manchester United vs Ipswich Town: Both Teams to Score",
            outcome="Yes",
            **common,
        )
        final_match = match(
            "target",
            "2026-08-30T15:00:00+00:00",
            "Manchester United",
            "Ipswich Town",
            (5, 2),
            ("4.8", "1.8"),
        )

        spread_result, btts_result = analyze_bets(
            [spread, btts], {event.event_slug: event}, [final_match]
        )

        self.assertEqual((spread_result.result, spread_result.hold_pnl), ("WIN", Decimal("6")))
        self.assertEqual((btts_result.result, btts_result.hold_pnl), ("WIN", Decimal("6")))
        self.assertEqual(spread_result.resolution_source, "Final-score fallback")
        self.assertEqual(btts_result.resolution_source, "Final-score fallback")
        self.assertEqual(
            (spread_result.direction_team, spread_result.direction_opponent),
            ("Manchester United", "Ipswich Town"),
        )
        self.assertIsNone(btts_result.direction_team)


if __name__ == "__main__":
    unittest.main()
