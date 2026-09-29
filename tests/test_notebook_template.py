from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import nbformat

from polymarket_bot.analytics_models import MarketEvent
from polymarket_bot.history import AnalyticsStore
from tests.test_notebook_data import USER, trade


TEMPLATE = Path(__file__).resolve().parents[1] / "notebooks" / "bet_history_analysis.ipynb"


class NotebookTemplateTests(unittest.TestCase):
    def test_template_is_valid_and_has_no_private_execution_artifacts(self):
        notebook = nbformat.read(TEMPLATE, as_version=4)
        nbformat.validate(notebook)
        self.assertEqual(set(notebook.metadata), {"kernelspec", "language_info"})
        for cell in notebook.cells:
            self.assertFalse(cell.get("outputs"))
            self.assertIsNone(cell.get("execution_count"))
            self.assertNotIn("execution", cell.metadata)
            self.assertNotIn("attachments", cell)
            self.assertNotRegex(cell.source, r"0x[a-fA-F0-9]{40}")
            if cell.cell_type == "code":
                compile(cell.source, str(TEMPLATE), "exec")

    def test_template_runs_for_empty_and_unmatched_ledgers(self):
        notebook = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        for populated in (False, True):
            with self.subTest(populated=populated), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                (project / "pyproject.toml").touch()
                with AnalyticsStore(project / "data" / "bet_history.sqlite3") as store:
                    store.remember_user(USER)
                    if populated:
                        now = datetime.now(timezone.utc)
                        fill = replace(trade(), timestamp=int(now.timestamp()),
                                       event_slug=f"epl-ful-che-{now:%Y-%m-%d}")
                        store.upsert_trades([fill])
                        store.upsert_event(MarketEvent(
                            fill.event_slug, "Fulham vs Chelsea", now,
                            "Fulham", "Chelsea", {fill.condition_id: "home"},
                            {fill.condition_id: "Yes"},
                        ))
                namespace = {}
                with patch.object(Path, "cwd", return_value=project), \
                     patch("IPython.display.display"), redirect_stdout(io.StringIO()):
                    for cell in notebook["cells"]:
                        if cell["cell_type"] == "code":
                            exec(compile("".join(cell["source"]), str(TEMPLATE), "exec"), namespace)
                self.assertEqual(len(namespace["trades"]), int(populated))
                self.assertEqual(len(namespace["performance"]["figures"]), 6)
                self.assertEqual(len(list((project / "data" / "performance_plots").glob("*.png"))), 6)


if __name__ == "__main__":
    unittest.main()
