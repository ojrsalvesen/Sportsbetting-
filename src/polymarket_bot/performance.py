"""Offline performance series and plots for the season-scoped betting ledger."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from polymarket_bot.errors import ConfigError
from polymarket_bot.notebook_data import _pandas
from polymarket_bot.reporting import normalize_team


def directional_performance(bets: Any) -> Any:
    """One equal-weight observation per completed fixture/team direction."""
    pd = _pandas()
    columns = [
        "match_id", "fixture", "kickoff", "direction_team", "direction_opponent",
        "goals_for", "goals_against", "xg_for", "xg_against", "goal_diff",
        "xg_diff", "outcome_overperformance", "selections", "buy_fills", "live_fills",
        "cum_goals_for", "cum_goals_against", "cum_goal_diff", "cum_xg_for",
        "cum_xg_against", "cum_xg_diff", "cum_outcome_overperformance",
    ]
    rows = bets.dropna(subset=[
        "match_id", "kickoff", "direction_team", "direction_opponent",
        "direction_goals", "opponent_goals", "direction_xg", "opponent_xg",
    ]).copy()
    if rows.empty:
        return pd.DataFrame(columns=columns)
    rows["team_key"] = rows.direction_team.map(normalize_team)
    rows["live_fill"] = rows.placed_at >= rows.kickoff
    # Contradictory source rows should never be hidden by taking the first value.
    values = ["direction_goals", "opponent_goals", "direction_xg", "opponent_xg"]
    if rows.groupby(["match_id", "team_key"])[values].nunique().gt(1).any().any():
        raise ConfigError("Conflicting score/xG values for the same fixture direction")
    frame = rows.groupby(["match_id", "team_key"], as_index=False).agg(
        fixture=("fixture", "first"), kickoff=("kickoff", "first"),
        direction_team=("direction_team", "first"),
        direction_opponent=("direction_opponent", "first"),
        goals_for=("direction_goals", "first"), goals_against=("opponent_goals", "first"),
        xg_for=("direction_xg", "first"), xg_against=("opponent_xg", "first"),
        selections=("selection_key", "nunique"), buy_fills=("trade_id", "count"),
        live_fills=("live_fill", "sum"),
    ).sort_values(["kickoff", "match_id", "team_key"]).reset_index(drop=True)
    frame["goal_diff"] = frame.goals_for - frame.goals_against
    frame["xg_diff"] = frame.xg_for - frame.xg_against
    frame["outcome_overperformance"] = frame.goal_diff - frame.xg_diff
    for column in ["goals_for", "goals_against", "goal_diff", "xg_for", "xg_against", "xg_diff", "outcome_overperformance"]:
        frame[f"cum_{column}"] = frame[column].cumsum()
    return frame[columns]


def realized_performance(trades: Any, bets: Any) -> dict[str, Any]:
    """Moving-average cost basis; sales plus remaining officially resolved holdings.

    Resolution dates are approximated by UTC fixture dates (or the last trade date
    if later), not claimed to be the exchange's actual redemption timestamps.
    Redemptions are not added again after recognizing their settlement value.
    """
    pd = _pandas()
    zero = Decimal("0")
    event_columns = ["date", "asset", "market", "kind", "pnl_usd", "released_cost_usd", "proceeds_usd"]
    open_columns = ["asset", "market", "shares", "remaining_cost_usd", "status"]
    events, holdings, warnings = [], [], []
    ordered = trades.sort_values(["placed_at", "id"])
    if trades.id.duplicated().any():
        raise ConfigError("Duplicate trade IDs in P/L input")
    for asset, group in ordered.groupby("asset", sort=False, dropna=False):
        if pd.isna(asset) or not str(asset).strip():
            raise ConfigError("Missing token ID prevents P/L reconstruction")
        shares = basis = zero
        asset_events = []
        market = str(group.iloc[-1].title)
        for trade in group.itertuples():
            size, cash = Decimal(str(trade.size)), Decimal(str(trade.usdc_size))
            if not size.is_finite() or not cash.is_finite() or size <= zero or cash < zero:
                raise ConfigError("Invalid share quantity or cash amount in P/L input")
            if trade.side == "BUY":
                shares += size
                basis += cash
            elif trade.side == "SELL":
                if size > shares + Decimal("0.000001"):
                    raise ConfigError(f"SELL exceeds known BUY inventory for {market}; import earlier history")
                sold = min(size, shares)
                cost = basis * sold / shares
                asset_events.append({
                    "date": trade.placed_at.normalize(), "asset": asset,
                    "market": market, "kind": "Sale", "pnl_usd": float(cash - cost),
                    "released_cost_usd": float(cost), "proceeds_usd": float(cash),
                })
                shares -= sold
                basis -= cost
            else:
                raise ConfigError(f"Unsupported P/L trade side: {trade.side}")
        selection = bets.loc[bets.asset.eq(asset)]
        official = selection.loc[
            selection.resolution_source.eq("Polymarket") & selection.result.isin(["WIN", "LOSS"])
        ]
        if not official.empty and official.result.nunique() != 1:
            raise ConfigError(f"Conflicting official outcomes for {market}")
        if shares > zero and not official.empty and official.kickoff.notna().any():
            outcome = official.iloc[0]
            payout = shares if outcome.result == "WIN" else zero
            date = max(official.kickoff.dropna().min().normalize(), group.placed_at.max().normalize())
            asset_events.append({
                "date": date, "asset": asset, "market": market, "kind": "Settlement",
                "pnl_usd": float(payout - basis), "released_cost_usd": float(basis),
                "proceeds_usd": float(payout),
            })
            shares = basis = zero
        if shares > zero:
            status = "Unresolved / unclassified"
            if selection.result.isin(["WIN", "LOSS"]).any():
                status = "Awaiting official settlement metadata"
                warnings.append(f"Excluded score-only/undated settlement from realized P/L: {market}")
            holdings.append({"asset": asset, "market": market, "shares": float(shares),
                             "remaining_cost_usd": float(basis), "status": status})
        events.extend(asset_events)
    event_frame = pd.DataFrame(events, columns=event_columns)
    event_frame["date"] = pd.to_datetime(event_frame.date, utc=True)
    daily = event_frame.groupby("date")[["pnl_usd", "released_cost_usd", "proceeds_usd"]].sum()
    if not trades.empty:
        # Include a zero baseline and days with no realized activity.
        start = trades.placed_at.min().normalize() - pd.Timedelta(days=1)
        end = max(trades.placed_at.max().normalize(), daily.index.max()) if not daily.empty else trades.placed_at.max().normalize()
        daily = daily.reindex(pd.date_range(start, end, freq="D", name="date"), fill_value=0)
    daily["cumulative_pnl_usd"] = daily.pnl_usd.cumsum()
    return {"events": event_frame.sort_values(["date", "asset"]).reset_index(drop=True),
            "daily": daily.reset_index(), "open_holdings": pd.DataFrame(holdings, columns=open_columns),
            "warnings": warnings}


def plot_performance_dashboard(data: dict[str, Any], *, output_directory: str | Path | None = None) -> dict[str, Any]:
    """Build six standalone plots and return the audit frames behind them."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import StrMethodFormatter
    import numpy as np
    from polymarket_bot.xg_benchmark import plot_xg_benchmark, xg_return_benchmark

    pd = _pandas()
    directions = directional_performance(data["bets"])
    pnl = realized_performance(data["trades"], data["bets"])
    figures = {}
    colors = {"for": "#21867a", "against": "#d47441", "actual": "#233c58", "xg": "#9470ac"}

    def style(ax, title, ylabel, *, dates=False):
        ax.set(title=title, ylabel=ylabel)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=.2)
        ax.grid(axis="x", visible=False)
        if dates:
            locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    fig, ax = plt.subplots(figsize=(12, 4.5), layout="constrained")
    daily = pnl["daily"]
    style(ax, f"1. Realized football P/L | {data['season']}/{str(data['season'] + 1)[-2:]}", "USD", dates=True)
    if daily.empty:
        ax.text(.5, .5, "No trade history available", transform=ax.transAxes, ha="center")
    else:
        ax.step(daily.date, daily.cumulative_pnl_usd, where="post", color=colors["actual"], lw=2)
        ax.fill_between(daily.date, daily.cumulative_pnl_usd, 0, step="post", alpha=.12, color=colors["actual"])
        value = daily.cumulative_pnl_usd.iloc[-1]
        ax.plot(daily.date.iloc[-1], value, "o", color=colors["actual"])
        ax.annotate(f"${value:+,.2f}", (daily.date.iloc[-1], value), xytext=(-8, 12),
                    textcoords="offset points", ha="right", weight="bold")
    ax.axhline(0, color="gray", lw=.8)
    ax.yaxis.set_major_formatter(StrMethodFormatter("${x:,.0f}"))
    ax.set_xlabel("Sales on trade dates; resolved holdings on UTC match dates. Open holdings are excluded.")
    figures["01_realized_pnl"] = fig

    # Daily aggregation avoids artificial ordering between simultaneous matches.
    if not directions.empty:
        dated = directions.assign(date=directions.kickoff.dt.normalize())
        sums = dated.groupby("date")[["goal_diff", "xg_diff", "xg_for", "xg_against"]].sum()
        baseline = pd.DataFrame(0., index=[sums.index.min() - pd.Timedelta(days=1)], columns=sums.columns)
        cumulative = pd.concat([baseline, sums]).sort_index().cumsum()
    else:
        cumulative = pd.DataFrame(columns=["goal_diff", "xg_diff", "xg_for", "xg_against"])

    fig, ax = plt.subplots(figsize=(12, 4.5), layout="constrained")
    style(ax, "2. Outcomes versus chances created", "Cumulative goal difference", dates=True)
    if cumulative.empty:
        ax.text(.5, .5, "No completed directional fixtures with xG", transform=ax.transAxes, ha="center")
    else:
        for column, label, color in [("goal_diff", "Actual goal difference", colors["actual"]),
                                     ("xg_diff", "Expected goal difference", colors["xg"])]:
            ax.step(cumulative.index, cumulative[column], where="post", label=f"{label}: {cumulative[column].iloc[-1]:+.2f}", color=color, lw=2)
        gap = cumulative.goal_diff - cumulative.xg_diff
        ax.fill_between(cumulative.index, cumulative.goal_diff, cumulative.xg_diff,
                        step="post", color=colors["xg"], alpha=.12)
        ax.text(.02, .04, f"Outcome overperformance: {gap.iloc[-1]:+.2f} goals", transform=ax.transAxes)
        ax.legend(loc="best", frameon=False)
    ax.axhline(0, color="gray", lw=.8)
    ax.set_xlabel("One observation per fixture/team direction; positive gap = outcomes above xG")
    figures["02_outcomes_vs_xg"] = fig

    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True, layout="constrained", gridspec_kw={"height_ratios": [2, 1]})
    style(axes[0], "3. Cumulative xG for and against", "Expected goals", dates=True)
    style(axes[1], "", "Net xG difference", dates=True)
    if cumulative.empty:
        axes[0].text(.5, .5, "No completed directional fixtures with xG", transform=axes[0].transAxes, ha="center")
    else:
        for column, label, color in [("xg_for", "xG for", colors["for"]), ("xg_against", "xG against", colors["against"])]:
            axes[0].step(cumulative.index, cumulative[column], where="post", lw=2, color=color,
                         label=f"{label}: {cumulative[column].iloc[-1]:.2f}")
        axes[0].legend(frameon=False)
        axes[1].step(cumulative.index, cumulative.xg_diff, where="post", lw=2, color=colors["actual"],
                     label=f"Net: {cumulative.xg_diff.iloc[-1]:+.2f}")
        axes[1].legend(frameon=False)
        axes[1].fill_between(cumulative.index, cumulative.xg_diff, 0, step="post", alpha=.12, color=colors["actual"])
    axes[1].axhline(0, color="gray", lw=.8)
    axes[1].set_xlabel("Equal weight per completed fixture/team direction")
    figures["03_cumulative_xg"] = fig

    fig, axes = plt.subplots(1, 2, figsize=(14, max(4, len(directions) * .4 + 1.7)), sharex=True, sharey=True, layout="constrained")
    if directions.empty:
        axes[0].text(.5, .5, "No completed directional fixtures", transform=axes[0].transAxes, ha="center")
    else:
        y = np.arange(len(directions))
        labels = [f"{row.kickoff:%d %b}  {row.direction_team} vs {row.direction_opponent}" for row in directions.itertuples()]
        for ax, goals, xg, title, color in [
            (axes[0], "goals_for", "xg_for", "4. Per match: for", colors["for"]),
            (axes[1], "goals_against", "xg_against", "Per match: against", colors["against"]),
        ]:
            ax.barh(y - .17, directions[goals], height=.32, label="Actual goals", color=color)
            ax.barh(y + .17, directions[xg], height=.32, label="xG", color=color, alpha=.35)
            ax.set(yticks=y, yticklabels=labels, xlabel="Goals / xG", title=title)
            ax.legend(frameon=False)
            ax.spines[["top", "right"]].set_visible(False)
        axes[0].invert_yaxis()
    figures["04_per_match_goals_xg"] = fig
    benchmark = xg_return_benchmark(data["bets"])
    figures.update(plot_xg_benchmark(benchmark))
    if output_directory is not None:
        directory = Path(output_directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, fig in figures.items():
            fig.savefig(directory / f"{name}.png", dpi=160, bbox_inches="tight")
        directions.to_csv(directory / "fixture_directions.csv", index=False)
        pnl["daily"].to_csv(directory / "realized_pnl_daily.csv", index=False)
        pnl["events"].to_csv(directory / "realized_pnl_events.csv", index=False)
        pnl["open_holdings"].to_csv(directory / "open_holdings.csv", index=False)
        for name in ["selections", "daily", "fill_audit", "exclusions"]:
            benchmark[name].to_csv(directory / f"xg_benchmark_{name}.csv", index=False)
    return {"figures": figures, "directions": directions, "pnl": pnl, "xg_benchmark": benchmark}
