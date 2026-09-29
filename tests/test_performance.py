from __future__ import annotations

import unittest

import pandas as pd

from polymarket_bot.errors import ConfigError
from polymarket_bot.notebook_data import BET_COLUMNS
from polymarket_bot.performance import directional_performance, realized_performance


def trades(rows):
    frame = pd.DataFrame(rows, columns=["id", "asset", "side", "size", "usdc_size", "placed_at"])
    frame["placed_at"] = pd.to_datetime(frame.placed_at, utc=True)
    frame["title"] = "Example market"
    return frame


def bet(**overrides):
    result = dict.fromkeys(BET_COLUMNS)
    result.update(trade_id="1", selection_key="a", asset="a", match_id="m1",
                  fixture="Liverpool vs Chelsea", direction_team="Liverpool",
                  direction_opponent="Chelsea", direction_goals=1, opponent_goals=2,
                  direction_xg=2.5, opponent_xg=.5,
                  placed_at=pd.Timestamp("2026-08-01", tz="UTC"),
                  kickoff=pd.Timestamp("2026-08-02 15:00", tz="UTC"),
                  result="WIN", resolution_source="Polymarket")
    result.update(overrides)
    return result


class RealizedPerformanceTests(unittest.TestCase):
    def test_partial_sale_and_settlement_use_remaining_inventory_and_recorded_cost(self):
        fills = trades([("1", "a", "BUY", 10, 5.2, "2026-08-01"),
                        ("2", "a", "SELL", 4, 2.8, "2026-08-02")])
        report = realized_performance(fills, pd.DataFrame([bet()]))
        # Sale: 2.8 - 4*.52 = .72; settlement: 6 - 6*.52 = 2.88.
        self.assertAlmostEqual(report["events"].pnl_usd.sum(), 3.6)
        self.assertAlmostEqual(report["events"].released_cost_usd.sum(), 5.2)
        self.assertEqual(len(report["open_holdings"]), 0)
        self.assertEqual(report["daily"].cumulative_pnl_usd.iloc[0], 0)
        self.assertEqual(len(report["daily"]), 3)

    def test_losing_unredeemed_position_counts_even_without_closed_record(self):
        fills = trades([("1", "a", "BUY", 10, 5.2, "2026-08-01")])
        report = realized_performance(fills, pd.DataFrame([bet(result="LOSS")]))
        self.assertAlmostEqual(report["daily"].cumulative_pnl_usd.iloc[-1], -5.2)
        self.assertEqual(report["events"].iloc[0]["kind"], "Settlement")

    def test_full_exit_is_not_paid_again_at_settlement(self):
        fills = trades([("1", "a", "BUY", 10, 5, "2026-08-01"),
                        ("2", "a", "SELL", 10, 6, "2026-08-02")])
        report = realized_performance(fills, pd.DataFrame([bet()]))
        self.assertEqual(report["events"].kind.tolist(), ["Sale"])
        self.assertAlmostEqual(report["events"].pnl_usd.sum(), 1)

    def test_buy_after_partial_sale_uses_moving_cost_basis(self):
        fills = trades([("1", "a", "BUY", 10, 4, "2026-08-01"),
                        ("2", "a", "SELL", 5, 3, "2026-08-02"),
                        ("3", "a", "BUY", 10, 8, "2026-08-03")])
        report = realized_performance(fills, pd.DataFrame([bet(kickoff=pd.Timestamp("2026-08-04", tz="UTC"))]))
        self.assertAlmostEqual(report["events"].pnl_usd.sum(), 6)  # 3 + 15 - 12

    def test_score_fallback_does_not_realize_and_open_purchase_is_not_a_loss(self):
        fills = trades([("1", "a", "BUY", 10, 5, "2026-08-01")])
        report = realized_performance(fills, pd.DataFrame([bet(resolution_source="Final-score fallback")]))
        self.assertEqual(report["daily"].cumulative_pnl_usd.iloc[-1], 0)
        self.assertEqual(report["open_holdings"].remaining_cost_usd.sum(), 5)
        self.assertTrue(report["warnings"])

    def test_missing_inventory_and_duplicate_fills_fail_loudly(self):
        fills = trades([("1", "a", "SELL", 10, 5, "2026-08-01")])
        with self.assertRaisesRegex(ConfigError, "inventory"):
            realized_performance(fills, pd.DataFrame([bet()]))
        fills = trades([("1", "a", "BUY", 10, 5, "2026-08-01")]*2)
        with self.assertRaisesRegex(ConfigError, "Duplicate"):
            realized_performance(fills, pd.DataFrame([bet()]))

    def test_empty_ledger(self):
        frame = trades([])
        report = realized_performance(frame, pd.DataFrame(columns=BET_COLUMNS))
        self.assertTrue(report["daily"].empty)
        self.assertTrue(directional_performance(pd.DataFrame(columns=BET_COLUMNS)).empty)

    def test_dust_sale_with_zero_inventory_fails_as_missing_history(self):
        fills = trades([("1", "a", "BUY", 10, 5, "2026-08-01"),
                        ("2", "a", "SELL", 10, 6, "2026-08-02"),
                        ("3", "a", "SELL", 0.000001, 0, "2026-08-03")])
        with self.assertRaisesRegex(ConfigError, "inventory"):
            realized_performance(fills, pd.DataFrame([bet()]))


class DirectionalPerformanceTests(unittest.TestCase):
    def test_fragmented_fills_and_multiple_handicaps_count_one_fixture_direction(self):
        frame = pd.DataFrame([
            bet(), bet(trade_id="2", direction_team="Liverpool FC"),
            bet(trade_id="3", selection_key="b", asset="b",
                placed_at=pd.Timestamp("2026-08-02 16:00", tz="UTC")),
            bet(trade_id="4", selection_key="c", asset="c", direction_team=None),
        ])
        result = directional_performance(frame)
        self.assertEqual(len(result), 1)
        row = result.iloc[0]
        self.assertEqual(row.selections, 2)
        self.assertEqual(row.buy_fills, 3)
        self.assertEqual(row.live_fills, 1)
        self.assertEqual(row.cum_xg_for, 2.5)
        self.assertEqual(row.cum_xg_against, .5)
        self.assertEqual(row.outcome_overperformance, -3)

    def test_opposite_directions_are_explicit_and_net_to_zero(self):
        frame = pd.DataFrame([bet(), bet(trade_id="2", selection_key="b", asset="b",
            direction_team="Chelsea", direction_opponent="Liverpool", direction_goals=2,
            opponent_goals=1, direction_xg=.5, opponent_xg=2.5)])
        result = directional_performance(frame)
        self.assertEqual(len(result), 2)
        self.assertEqual(result.cum_xg_diff.iloc[-1], 0)
        self.assertEqual(result.cum_goal_diff.iloc[-1], 0)

    def test_conflicting_xg_cannot_be_hidden_by_deduplication(self):
        with self.assertRaisesRegex(ConfigError, "Conflicting"):
            directional_performance(pd.DataFrame([bet(), bet(trade_id="2", direction_xg=3)]))


if __name__ == "__main__":
    unittest.main()
