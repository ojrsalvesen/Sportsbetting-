"""Build a plots-only view of the existing local betting-analysis notebook."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from pathlib import Path
import shutil

import nbformat


TAG = "bet-performance-dashboard"
DATA_TAG = "bet-analysis-data-preparation"


def quiet_source(source: str) -> str:
    """Keep calculations verbatim while removing standalone display/print calls."""
    lines = source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in {"display", "print"}):
            for index in range(node.lineno - 1, node.end_lineno):
                lines[index] = ""
    return "".join(lines).strip()


def hidden_code(source: str, tag: str):
    cell = nbformat.v4.new_code_cell(source)
    cell.metadata.update(tags=[tag, "hide-input"], jupyter={"source_hidden": True})
    return cell


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notebook", type=Path, default=Path("notebooks/bet_history_analysis.clean.local.ipynb"))
    args = parser.parse_args()
    notebook = nbformat.read(args.notebook, as_version=4)
    prepared = [cell.source for cell in notebook.cells
                if DATA_TAG in cell.metadata.get("tags", [])]
    if prepared:
        preparation = prepared[0]
    else:
        # Retain useful DataFrames, omitting legacy plots and display-only cells.
        markers = [
            "from datetime import datetime, timezone", "season_start = pd.Timestamp",
            "def single_value(values):", "fixture_keys =", "settled = selections.dropna",
            "market_summary = (", "fixture_xg = xg_matches", "directional_xg = selections",
        ]
        sources = []
        for marker in markers:
            cell = next((cell for cell in notebook.cells
                         if cell.cell_type == "code" and cell.source.startswith(marker)), None)
            if cell is None:
                raise ValueError(f"Expected data-preparation cell is missing: {marker}")
            sources.append(quiet_source(cell.source))
        preparation = "\n\n".join(sources)
    plot_setup = """from polymarket_bot.performance import plot_performance_dashboard

performance = plot_performance_dashboard(data, output_directory=PROJECT_ROOT / 'data' / 'performance_plots')
directional_matches = performance['directions']
pnl_events = performance['pnl']['events']
pnl_daily = performance['pnl']['daily']
open_holdings = performance['pnl']['open_holdings']
performance_warnings = performance['pnl']['warnings']
xg_benchmark_selections = performance['xg_benchmark']['selections']
xg_benchmark_daily = performance['xg_benchmark']['daily']
xg_benchmark_fill_audit = performance['xg_benchmark']['fill_audit']
xg_benchmark_exclusions = performance['xg_benchmark']['exclusions']

# DataFrames remain available for manual inspection, including bets, trades,
# selections, xg_matches, scope_checks, kpis, market_summary, directional_matches,
# pnl_events, pnl_daily, open_holdings, and the xg_benchmark_* frames.
for figure in performance['figures'].values():
    plt.close(figure)
"""
    cells = [
        nbformat.v4.new_markdown_cell("# Betting performance\n\nEPL season analysis · P/L, outcomes versus xG, and chance creation."),
        hidden_code(preparation, DATA_TAG),
        hidden_code(plot_setup, TAG),
    ]
    for name in ["01_realized_pnl", "02_outcomes_vs_xg", "03_cumulative_xg", "04_per_match_goals_xg",
                 "05_xg_benchmark_roi", "06_xg_benchmark_pnl"]:
        cells.append(hidden_code(f"display(performance['figures']['{name}'])", TAG))
    backup_dir = args.notebook.resolve().parents[1] / "data" / "notebook_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{args.notebook.stem}.{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.ipynb"
    shutil.copy2(args.notebook, backup)
    notebook.cells = cells
    nbformat.validate(notebook)
    nbformat.write(notebook, args.notebook)
    print(f"Saved plots-only notebook: {args.notebook}; previous version: {backup}")


if __name__ == "__main__":
    main()
