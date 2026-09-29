from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from polymarket_bot import analytics_cli, report_cli
from polymarket_bot.analytics_models import BetTrade, MarketEvent
from polymarket_bot.errors import ApiError
from polymarket_bot.history import AnalyticsStore
from tests.test_history import USER, trade_payload


class AnalyticsCliTests(unittest.TestCase):
    def test_sync_failures_are_visible_and_return_nonzero_even_with_cached_trades(self):
        for failing_source in ("trades", "positions", "events", "xg", None):
            with self.subTest(source=failing_source), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "ledger.sqlite3"
                trade = BetTrade.from_api(trade_payload(), user=USER)
                event = MarketEvent(trade.event_slug, "Fulham vs Chelsea", trade.placed_at,
                                    "Fulham", "Chelsea", {trade.condition_id: "home"})
                with AnalyticsStore(database) as store:
                    store.remember_user(USER)
                    store.upsert_trades([trade])
                    store.upsert_event(event)
                output = io.StringIO()
                with patch.object(analytics_cli, "PolymarketHistoryClient") as history, \
                     patch.object(analytics_cli, "UnderstatXgClient") as xg, redirect_stdout(output):
                    client = history.return_value.__enter__.return_value
                    client.trades_for_season.return_value = [trade]
                    client.closed_positions_for_season.return_value = []
                    client.event.return_value = event
                    xg_client = xg.return_value.__enter__.return_value
                    xg_client.matches.return_value = []
                    methods = {"trades": client.trades_for_season,
                               "positions": client.closed_positions_for_season,
                               "events": client.event, "xg": xg_client.matches}
                    if failing_source:
                        methods[failing_source].side_effect = ApiError("test provider unavailable")
                        with self.assertRaises(SystemExit) as raised:
                            analytics_cli.main(["--database", str(database), "--season", "2026"])
                        self.assertEqual(raised.exception.code, 2)
                    else:
                        analytics_cli.main(["--database", str(database), "--season", "2026"])
                rendered = output.getvalue()
                self.assertIn("INCOMPLETE SYNC" if failing_source else "SYNC COMPLETE", rendered)
                self.assertIn("Latest stored trade: 2026-08-24", rendered)
                self.assertIn("1 trades stored", rendered)

    def test_offline_is_explicit_and_does_not_create_network_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "ledger.sqlite3"
            with AnalyticsStore(database) as store:
                store.remember_user(USER)
            with patch.object(analytics_cli, "PolymarketHistoryClient") as history, \
                 patch.object(analytics_cli, "UnderstatXgClient") as xg, redirect_stdout(io.StringIO()) as output:
                analytics_cli.main(["--database", str(database), "--offline"])
            history.assert_not_called()
            xg.assert_not_called()
            self.assertIn("OFFLINE: cached data only", output.getvalue())
            self.assertIn("Latest stored trade: none", output.getvalue())

    def test_nonfinite_timeouts_and_invalid_environment_defaults_are_parser_errors(self):
        for module in (analytics_cli, report_cli):
            for value in ("nan", "inf", "0"):
                with self.subTest(module=module.__name__, value=value), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        module._parser().parse_args(["--timeout-seconds", value])
                    self.assertEqual(raised.exception.code, 2)
        with patch.dict("os.environ", {"PMA_REFRESH_HOURS": "nan"}), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                analytics_cli._parser().parse_args([])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
