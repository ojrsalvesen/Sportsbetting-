from __future__ import annotations

import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from polymarket_bot.errors import ConfigError
from polymarket_bot.notebook_data import BET_COLUMNS
from polymarket_bot.performance import plot_performance_dashboard
from polymarket_bot.xg_benchmark import (
    UnsupportedMarket, market_rule, plot_xg_benchmark, poisson_probabilities,
    score_grid, selection_probability, xg_return_benchmark,
)


def bet(**overrides):
    row = dict.fromkeys(BET_COLUMNS)
    row.update(trade_id="1", selection_key="a", asset="a", match_id="m1",
               fixture="Liverpool vs Chelsea", market_title="Will Liverpool FC win on 2026-08-02?",
               market_type="Full-time 1X2", market_role="home", selection="YES",
               home_team="Liverpool FC", away_team="Chelsea FC", home_goals=2, away_goals=0,
               post_home_xg=2., post_away_xg=.8, shares=10., cost_usd=5.2, entry_price=.5,
               placed_at=pd.Timestamp("2026-08-01", tz="UTC"),
               kickoff=pd.Timestamp("2026-08-02 15:00", tz="UTC"),
               result="WIN", resolution_source="Polymarket")
    row.update(overrides)
    return row


def rule(**overrides):
    return market_rule(SimpleNamespace(**bet(**overrides)))


class ScoreProbabilityTests(unittest.TestCase):
    def test_probability_mass_and_explicit_tail_bound(self):
        for mean in [0., .001, .8, 2., 12., 50.]:
            pmf, tail = poisson_probabilities(mean)
            self.assertLessEqual(tail, 5e-13)
            self.assertLessEqual(abs(1 - math.fsum(pmf)), tail + 1e-13)
        for home, away in [(0., 0.), (2., .8), (12., 10.)]:
            grid, tail = score_grid(home, away)
            self.assertAlmostEqual(math.fsum(map(math.fsum, grid)), 1., places=14)
            self.assertLessEqual(tail, 1e-12)

    def test_invalid_xg_and_tolerance(self):
        for value in [-1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                poisson_probabilities(value)
        for value in [0, -1, 1]:
            with self.assertRaises(ValueError):
                score_grid(1., 1., value)

    def test_three_outcomes_partition_and_yes_no_complement(self):
        total = 0.
        for role in ["home", "away", "draw"]:
            yes, _ = selection_probability(rule(market_role=role), 2., .8)
            no, _ = selection_probability(rule(market_role=role, selection="NO"), 2., .8)
            self.assertAlmostEqual(yes + no, 1., places=14)
            total += yes
        self.assertAlmostEqual(total, 1., places=14)

    def test_home_away_symmetry_and_zero_xg_draw(self):
        home, _ = selection_probability(rule(), 2., .8)
        away, _ = selection_probability(rule(market_role="away"), .8, 2.)
        self.assertAlmostEqual(home, away, places=14)
        self.assertEqual(selection_probability(rule(market_role="draw"), 0., 0.)[0], 1.)
        self.assertEqual(selection_probability(rule(), 0., 0.)[0], 0.)

    def test_btts_matches_analytic_probability(self):
        title = "Liverpool FC vs. Chelsea FC: Both Teams to Score"
        yes = rule(market_type="Both teams to score", market_title=title)
        no = rule(market_type="Both teams to score", market_title=title, selection="NO")
        probability = selection_probability(yes, 2., .8)[0]
        self.assertAlmostEqual(probability, (1 - math.exp(-2)) * (1 - math.exp(-.8)), places=11)
        self.assertAlmostEqual(probability + selection_probability(no, 2., .8)[0], 1., places=14)

    def test_handicap_threshold_and_opposing_team_complement(self):
        args = dict(market_type="Spread", market_title="Spread: Liverpool FC (-1.5)", selection="LIVERPOOL FC")
        favourite = rule(**args)
        opponent = rule(**{**args, "selection": "CHELSEA FC"})
        self.assertFalse(favourite.wins(1, 0))
        self.assertTrue(favourite.wins(2, 0))
        self.assertEqual(opponent.label, "Chelsea FC +1.5")
        self.assertAlmostEqual(selection_probability(favourite, 2., .8)[0] + selection_probability(opponent, 2., .8)[0], 1., places=14)
        harder = rule(**{**args, "market_title": "Spread: Liverpool FC (−2.5)"})
        self.assertLess(selection_probability(harder, 2., .8)[0], selection_probability(favourite, 2., .8)[0])
        self.assertEqual(rule(**{**args, "selection": "NO"}), opponent)

    def test_away_handicap_and_positive_line(self):
        away = rule(market_type="Spread", market_title="Spread: Chelsea FC (+0.5)", selection="Chelsea")
        self.assertTrue(away.wins(1, 1))
        self.assertFalse(away.wins(1, 0))
        opposite = rule(market_type="Spread", market_title="Spread: Chelsea FC (+0.5)", selection="Liverpool")
        self.assertEqual(opposite.label, "Liverpool FC -0.5")

    def test_ambiguous_and_unsupported_markets_fail_closed(self):
        examples = [
            dict(market_type="Season winner"), dict(market_type="Totals"),
            dict(market_role="unknown"), dict(selection="unknown"),
            dict(market_title="Liverpool to win first half"),
            dict(market_type="Both teams to score", market_title="BTTS extra time"),
            dict(market_type="Both teams to score", market_title="Liverpool vs Chelsea: Both Teams to Score - first half"),
            dict(market_type="Spread", market_title="Spread: Liverpool FC (-1)"),
            dict(market_type="Spread", market_title="Spread: Liverpool FC (-1.25)"),
            dict(market_type="Spread", market_title="Spread: Arsenal FC (-1.5)"),
            dict(market_type="Spread", market_title="Spread: Liverpool FC (-1.5)", selection="Arsenal"),
        ]
        for values in examples:
            with self.subTest(values=values), self.assertRaises(UnsupportedMarket):
                rule(**values)


class ReturnBenchmarkTests(unittest.TestCase):
    def test_recorded_cost_includes_fees_and_fragments_are_share_weighted(self):
        data = pd.DataFrame([bet(), bet(trade_id="2", shares=5., cost_usd=3.1, entry_price=.6)])
        report = xg_return_benchmark(data)
        row = report["selections"].iloc[0]
        self.assertEqual(row.buy_fills, 2)
        self.assertEqual(row.shares, 15.)
        self.assertAlmostEqual(row.cost_usd, 8.3)
        self.assertAlmostEqual(row.break_even_probability, 8.3 / 15)
        self.assertAlmostEqual(row.xg_edge, row.p_xg - 8.3 / 15)
        self.assertAlmostEqual(row.benchmark_pnl_usd, 15 * row.p_xg - 8.3)
        self.assertAlmostEqual(row.benchmark_roi, (15 * row.p_xg - 8.3) / 8.3)
        self.assertAlmostEqual(row.observed_hold_pnl_usd, 6.7)
        self.assertAlmostEqual(row.outcome_deviation_usd, 15 * (1 - row.p_xg))

    def test_live_fill_excluded_without_discarding_pre_match_fills(self):
        report = xg_return_benchmark(pd.DataFrame([bet(), bet(trade_id="2", placed_at=bet()["kickoff"])]))
        self.assertEqual(report["selections"].iloc[0].buy_fills, 1)
        self.assertEqual(report["selections"].iloc[0].cost_usd, 5.2)
        self.assertEqual(report["exclusions"].trade_id.tolist(), ["2"])
        self.assertIn("Live BUY", report["exclusions"].iloc[0].reason)

    def test_different_lines_remain_separate_and_daily_curves_share_cohort(self):
        rows = [bet(), bet(trade_id="2", selection_key="b", asset="b", market_type="Spread",
                          market_title="Spread: Liverpool FC (-2.5)", selection="Liverpool FC", result="LOSS"),
                bet(trade_id="3", selection_key="c", result="PENDING")]
        report = xg_return_benchmark(pd.DataFrame(rows))
        selections, daily = report["selections"], report["daily"]
        self.assertEqual(len(selections), 2)
        self.assertEqual(len(daily), 2)  # zero baseline + one match day
        for column in ["cost_usd", "benchmark_pnl_usd", "observed_hold_pnl_usd", "outcome_deviation_usd"]:
            self.assertEqual(daily.iloc[0][f"cumulative_{column}"], 0)
            self.assertAlmostEqual(daily.iloc[-1][f"cumulative_{column}"], selections[column].sum())
        self.assertAlmostEqual(daily.iloc[-1].cumulative_outcome_deviation_usd,
                               daily.iloc[-1].cumulative_observed_hold_pnl_usd - daily.iloc[-1].cumulative_benchmark_pnl_usd)
        self.assertAlmostEqual(selections.observed_hold_pnl_usd.sum(), -.4)

    def test_explicit_exclusion_reasons(self):
        examples = [
            (dict(resolution_source="Final-score fallback"), "Not officially settled"),
            (dict(post_home_xg=None), "Missing completed"),
            (dict(home_goals=None), "Missing completed"),
            (dict(kickoff=pd.NaT), "Missing kickoff"),
            (dict(post_home_xg=-1), "Invalid score/xG"),
            (dict(post_home_xg=float("inf")), "Invalid score/xG"),
            (dict(home_goals=1.5), "non-integer"),
            (dict(cost_usd=0), "Invalid purchase"),
            (dict(shares=float("inf")), "Invalid purchase"),
            (dict(result="LOSS"), "Official outcome disagrees"),
            (dict(selection_key=None), "Missing selection"),
        ]
        for values, reason in examples:
            with self.subTest(values=values):
                report = xg_return_benchmark(pd.DataFrame([bet(**values)]))
                self.assertTrue(report["selections"].empty)
                self.assertIn(reason, report["exclusions"].iloc[0].reason)

    def test_duplicate_ids_and_conflicting_sources_raise(self):
        for second in [bet(), bet(trade_id="2", post_home_xg=3),
                       bet(trade_id="2", selection_key="b", post_home_xg=3)]:
            with self.assertRaises(ConfigError):
                xg_return_benchmark(pd.DataFrame([bet(), second]))

    def test_empty_input_and_empty_plots(self):
        report = xg_return_benchmark(pd.DataFrame(columns=BET_COLUMNS))
        for value in report.values():
            self.assertTrue(value.empty)
        figures = plot_xg_benchmark(report)
        self.assertEqual(len(figures), 2)
        for fig in figures.values():
            fig.canvas.draw()
            plt.close(fig)

    def test_populated_plot_lines_match_same_cohort_and_title_is_not_duplicated(self):
        report = xg_return_benchmark(pd.DataFrame([bet()]))
        figures = plot_xg_benchmark(report)
        ax = figures["06_xg_benchmark_pnl"].axes[0]
        self.assertEqual(ax.get_title(loc="center"), "")
        self.assertIn("Outcome deviation", ax.get_title(loc="left"))
        self.assertEqual(list(ax.lines[0].get_ydata()), report["daily"].cumulative_observed_hold_pnl_usd.tolist())
        self.assertEqual(list(ax.lines[1].get_ydata()), report["daily"].cumulative_benchmark_pnl_usd.tolist())
        for fig in figures.values():
            fig.canvas.draw()
            plt.close(fig)

    def test_dashboard_exports_six_figures_and_benchmark_csvs(self):
        # Exercise the complete integration with an empty but correctly typed ledger.
        trades = pd.DataFrame(columns=["id", "asset", "side", "size", "usdc_size", "placed_at", "title"])
        trades["placed_at"] = pd.to_datetime(trades.placed_at, utc=True)
        with tempfile.TemporaryDirectory() as directory:
            report = plot_performance_dashboard({"bets": pd.DataFrame(columns=BET_COLUMNS),
                                                 "trades": trades, "season": 2026}, output_directory=directory)
            self.assertEqual(len(list(Path(directory).glob("*.png"))), 6)
            self.assertEqual(len(list(Path(directory).glob("xg_benchmark_*.csv"))), 4)
            for fig in report["figures"].values():
                plt.close(fig)


if __name__ == "__main__":
    unittest.main()
