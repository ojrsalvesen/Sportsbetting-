"""Retrospective, price-aware xG benchmarks for pre-match BUY fills.

This is a hold-to-settlement diagnostic, not an entry-time forecast or account P/L.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
import math
import re
from typing import Any

from polymarket_bot.analysis import SPREAD_PATTERN
from polymarket_bot.errors import ConfigError
from polymarket_bot.notebook_data import _pandas
from polymarket_bot.reporting import normalize_team


class UnsupportedMarket(ValueError):
    """A selection whose settlement rule cannot be scored unambiguously."""


@dataclass(frozen=True)
class MarketRule:
    kind: str
    role: str
    line: float
    invert: bool
    label: str

    def wins(self, home: int, away: int) -> bool:
        if self.kind == "1x2":
            won = home == away if self.role == "draw" else (home > away if self.role == "home" else away > home)
        elif self.kind == "btts":
            won = home > 0 and away > 0
        else:
            difference = home - away if self.role == "home" else away - home
            won = difference + self.line > 0
        return not won if self.invert else won


def market_rule(row: Any) -> MarketRule:
    """Resolve YES/NO and named-team half-goal handicaps, failing closed."""
    title, selection = str(row.market_title).strip(), str(row.selection).strip()
    if re.search(r"\b(half|period|extra\s*time|penalt\w*|1h|2h|ht)\b", title, re.I):
        raise UnsupportedMarket("Unsupported period or settlement rule")
    if row.market_type == "Full-time 1X2":
        if row.market_role not in ("home", "away", "draw") or selection.upper() not in ("YES", "NO"):
            raise UnsupportedMarket("Ambiguous 1X2 selection")
        label = "Draw" if row.market_role == "draw" else str(row.home_team if row.market_role == "home" else row.away_team) + " win"
        return MarketRule("1x2", row.market_role, 0, selection.upper() == "NO", f"{label}: {selection.upper()}")
    if row.market_type == "Both teams to score":
        if not re.fullmatch(r".+\s+vs\.?\s+.+:\s*Both Teams to Score\??", title, re.I) or selection.upper() not in ("YES", "NO"):
            raise UnsupportedMarket("Ambiguous full-match BTTS selection")
        return MarketRule("btts", "", 0, selection.upper() == "NO", f"BTTS: {selection.upper()}")
    if row.market_type == "Spread":
        match = SPREAD_PATTERN.fullmatch(title)
        if not match:
            raise UnsupportedMarket("Ambiguous handicap title")
        line = Decimal(match.group("line").replace("−", "-"))
        if abs(line) % 1 != Decimal("0.5"):
            raise UnsupportedMarket("Unsupported whole/quarter-goal handicap")
        home, away = normalize_team(str(row.home_team)), normalize_team(str(row.away_team))
        team = normalize_team(match.group("team"))
        if not home or not away or home == away or team not in (home, away):
            raise UnsupportedMarket("Handicap team does not match fixture")
        if selection.upper() in ("YES", "NO"):
            invert = selection.upper() == "NO"
        else:
            selected = normalize_team(selection)
            if selected not in (home, away):
                raise UnsupportedMarket("Ambiguous handicap selection")
            invert = selected != team
        named_home = team == home
        selected_team = row.home_team if named_home != invert else row.away_team
        selected_line = -float(line) if invert else float(line)
        return MarketRule("spread", "home" if named_home else "away", float(line), invert,
                          f"{selected_team} {selected_line:+g}")
    raise UnsupportedMarket("Unsupported market type")


def poisson_probabilities(mean: float, tolerance: float = 5e-13) -> tuple[tuple[float, ...], float]:
    """Unnormalised PMF plus a geometric upper bound on its omitted right tail."""
    if not math.isfinite(mean) or mean < 0:
        raise ValueError("xG must be finite and nonnegative")
    if not 0 < tolerance < 1:
        raise ValueError("Tail tolerance must lie between zero and one")
    if mean == 0:
        return (1.,), 0.
    values = []
    for n in range(10000):
        probability = math.exp(-mean + n * math.log(mean) - math.lgamma(n + 1))
        values.append(probability)
        if n + 2 > mean:
            next_probability = probability * mean / (n + 1)
            tail_bound = next_probability / (1 - mean / (n + 2))
            if tail_bound <= tolerance:
                return tuple(values), tail_bound
    raise ValueError("xG too large for the score-grid safety limit")


@lru_cache(maxsize=128)
def score_grid(home_xg: float, away_xg: float, tolerance: float = 1e-12) -> tuple[tuple[tuple[float, ...], ...], float]:
    """Independent Poisson scores; renormalise only after bounding omitted mass."""
    if not 0 < tolerance < 1:
        raise ValueError("Tail tolerance must lie between zero and one")
    home, home_tail = poisson_probabilities(home_xg, tolerance / 2)
    away, away_tail = poisson_probabilities(away_xg, tolerance / 2)
    mass = math.fsum(home) * math.fsum(away)
    return tuple(tuple(h * a / mass for a in away) for h in home), home_tail + away_tail


def selection_probability(rule: MarketRule, home_xg: float, away_xg: float) -> tuple[float, float]:
    grid, tail_bound = score_grid(home_xg, away_xg)
    probability = math.fsum(p for h, row in enumerate(grid) for a, p in enumerate(row) if rule.wins(h, a))
    return min(1., max(0., probability)), tail_bound


SELECTION_COLUMNS = [
    "selection_key", "match_id", "fixture", "kickoff", "market_title", "selection_label",
    "result", "buy_fills", "shares", "cost_usd", "home_xg", "away_xg", "p_xg",
    "score_tail_bound", "break_even_probability", "xg_edge", "benchmark_pnl_usd",
    "benchmark_roi", "observed_hold_pnl_usd", "outcome_deviation_usd",
]
DAILY_VALUES = ["cost_usd", "benchmark_pnl_usd", "observed_hold_pnl_usd", "outcome_deviation_usd"]


def xg_return_benchmark(bets: Any) -> dict[str, Any]:
    """Same-cohort benchmark/observed returns, with one audit entry per BUY fill.

    `bets` is the enriched BUY-only frame from load_notebook_data. SELLs are not
    inputs: both return series deliberately assume every eligible share was held.
    """
    pd = _pandas()
    if bets.trade_id.isna().any() or bets.trade_id.duplicated().any():
        raise ConfigError("Missing or duplicate BUY trade IDs in xG benchmark")
    eligible, audit = [], []
    for row in bets.itertuples(index=False):
        reason, rule = None, None
        try:
            rule = market_rule(row)
        except UnsupportedMarket as exc:
            reason = str(exc)
        if reason is None:
            if pd.isna(row.selection_key) or not str(row.selection_key).strip():
                reason = "Missing selection token"
            elif pd.isna(row.kickoff) or pd.isna(row.placed_at):
                reason = "Missing kickoff or purchase timestamp"
            elif row.placed_at >= row.kickoff:
                reason = "Live BUY fill (at/after kickoff)"
            elif row.resolution_source != "Polymarket" or row.result not in ("WIN", "LOSS"):
                reason = "Not officially settled"
            elif any(pd.isna(v) for v in (row.match_id, row.home_goals, row.away_goals, row.post_home_xg, row.post_away_xg)):
                reason = "Missing completed match score/xG"
            elif any(not math.isfinite(float(v)) or float(v) < 0 for v in (row.post_home_xg, row.post_away_xg, row.home_goals, row.away_goals)):
                reason = "Invalid score/xG"
            elif any(float(v) != int(v) for v in (row.home_goals, row.away_goals)):
                reason = "Invalid non-integer final score"
            elif any(pd.isna(v) or not math.isfinite(float(v)) or float(v) <= 0 for v in (row.shares, row.cost_usd)):
                reason = "Invalid purchase shares/cost"
            elif rule.wins(int(row.home_goals), int(row.away_goals)) != (row.result == "WIN"):
                reason = "Official outcome disagrees with parsed score rule"
        audit.append({"trade_id": row.trade_id, "selection_key": row.selection_key,
                      "market_title": row.market_title, "included": reason is None,
                      "reason": reason or "Included", "shares": row.shares, "cost_usd": row.cost_usd})
        if reason is None:
            eligible.append(row._asdict())
    records = []
    if eligible:
        frame = pd.DataFrame(eligible)
        consistency = ["match_id", "kickoff", "home_team", "away_team", "market_title", "market_type",
                       "market_role", "selection", "result", "home_goals", "away_goals", "post_home_xg", "post_away_xg"]
        if frame.groupby("selection_key")[consistency].nunique(dropna=False).gt(1).any().any():
            raise ConfigError("Conflicting source values for an xG benchmark selection")
        if frame.groupby("match_id")[["home_team", "away_team", "kickoff", "home_goals", "away_goals", "post_home_xg", "post_away_xg"]].nunique(dropna=False).gt(1).any().any():
            raise ConfigError("Conflicting score/xG values for an xG benchmark fixture")
        for key, group in frame.groupby("selection_key", sort=False):
            row = next(group.itertuples(index=False))
            rule = market_rule(row)
            probability, tail_bound = selection_probability(rule, float(row.post_home_xg), float(row.post_away_xg))
            shares = math.fsum(group.shares)
            cost = math.fsum(group.cost_usd)
            benchmark = shares * probability - cost
            observed = shares * (row.result == "WIN") - cost
            records.append({"selection_key": key, "match_id": row.match_id, "fixture": row.fixture,
                            "kickoff": row.kickoff, "market_title": row.market_title, "selection_label": rule.label,
                            "result": row.result, "buy_fills": len(group), "shares": shares, "cost_usd": cost,
                            "home_xg": row.post_home_xg, "away_xg": row.post_away_xg, "p_xg": probability,
                            "score_tail_bound": tail_bound, "break_even_probability": cost / shares,
                            "xg_edge": probability - cost / shares, "benchmark_pnl_usd": benchmark,
                            "benchmark_roi": benchmark / cost, "observed_hold_pnl_usd": observed,
                            "outcome_deviation_usd": observed - benchmark})
    selections = pd.DataFrame(records, columns=SELECTION_COLUMNS).sort_values(["kickoff", "match_id", "selection_key"]).reset_index(drop=True)
    selections["kickoff"] = pd.to_datetime(selections.kickoff, utc=True)
    daily = selections.assign(date=selections.kickoff.dt.normalize()).groupby("date")[DAILY_VALUES].sum()
    if not daily.empty:
        daily = daily.reindex(pd.date_range(daily.index.min() - pd.Timedelta(days=1), daily.index.max(), freq="D", name="date"), fill_value=0)
    for column in DAILY_VALUES:
        daily[f"cumulative_{column}"] = daily[column].cumsum()
    audit_frame = pd.DataFrame(audit, columns=["trade_id", "selection_key", "market_title", "included", "reason", "shares", "cost_usd"])
    exclusions = audit_frame.loc[audit_frame.included.eq(False)].reset_index(drop=True)
    return {"selections": selections, "daily": daily.reset_index(), "fill_audit": audit_frame, "exclusions": exclusions}


def plot_xg_benchmark(benchmark: dict[str, Any]) -> dict[str, Any]:
    """Two standalone plots; DataFrames are returned separately, never displayed."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.ticker import PercentFormatter, StrMethodFormatter
    import numpy as np

    selections, daily = benchmark["selections"], benchmark["daily"]
    teal, orange, navy, purple = "#21867a", "#d47441", "#233c58", "#9470ac"
    note = (f"{len(selections)} selections | {len(benchmark['exclusions'])} excluded BUY fills | "
            "Pre-match purchases; official settlements; retrospective full-match xG")
    figures = {}
    fig, ax = plt.subplots(figsize=(14, max(4.5, len(selections) * .38 + 2)), layout="constrained")
    ax.set_title("5. xG benchmark return versus your entry price", loc="left")
    if selections.empty:
        ax.text(.5, .5, "No eligible settled pre-match selections", transform=ax.transAxes, ha="center")
    else:
        y = np.arange(len(selections))
        labels = [f"{r.kickoff:%d %b}  {r.fixture}\n{r.selection_label}" for r in selections.itertuples()]
        ax.barh(y, selections.benchmark_roi, height=.65,
                color=[teal if value >= 0 else orange for value in selections.benchmark_roi])
        # Outcomes use shapes in a separate margin, not the ROI colour scale.
        for result, marker in [("WIN", "o"), ("LOSS", "x")]:
            mask = selections.result.eq(result).to_numpy()
            ax.scatter(np.full(mask.sum(), 1.025), y[mask], marker=marker, color=navy, s=32,
                       transform=ax.get_yaxis_transform(), clip_on=False)
        ax.set(yticks=y, yticklabels=labels)
        ax.tick_params(axis="y", labelsize=8)
        ax.invert_yaxis()
        ax.legend(handles=[Line2D([], [], marker="o", color=navy, ls="", label="Won"),
                           Line2D([], [], marker="x", color=navy, ls="", label="Lost")],
                  loc="lower right", bbox_to_anchor=(1.05, 1.015), frameon=False, ncol=2)
    ax.axvline(0, color="gray", lw=.8)
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.grid(axis="x", alpha=.2)
    ax.grid(axis="y", visible=False)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_xlabel("xG benchmark ROI = (shares × xG probability − purchase cost) / purchase cost\n" + note)
    figures["05_xg_benchmark_roi"] = fig

    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")
    ax.set_ylabel("USD")
    ax.set_title("6. Hold-to-settlement P/L versus xG benchmark", loc="left")
    if daily.empty:
        ax.text(.5, .5, "No eligible settled pre-match selections", transform=ax.transAxes, ha="center")
    else:
        actual, expected = daily.cumulative_observed_hold_pnl_usd, daily.cumulative_benchmark_pnl_usd
        ax.step(daily.date, actual, where="post", color=navy, lw=2, label=f"Observed hold P/L: ${actual.iloc[-1]:+,.2f}")
        ax.step(daily.date, expected, where="post", color=purple, lw=2, label=f"xG benchmark: ${expected.iloc[-1]:+,.2f}")
        ax.fill_between(daily.date, actual, expected, step="post", color=purple, alpha=.12)
        ax.legend(frameon=False, loc="best")
        ax.set_title(f"6. Hold-to-settlement P/L versus xG benchmark\nOutcome deviation: ${actual.iloc[-1] - expected.iloc[-1]:+,.2f}", loc="left")
    locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.yaxis.set_major_formatter(StrMethodFormatter("${x:,.0f}"))
    ax.axhline(0, color="gray", lw=.8)
    ax.grid(axis="y", alpha=.2)
    ax.grid(axis="x", visible=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_xlabel("Same purchased shares held to settlement; UTC match dates. Not realized account P/L.\n" + note
                  + "\nIndependent Poisson score approximation from home/away xG; gap is not proof of luck or skill.")
    figures["06_xg_benchmark_pnl"] = fig
    return figures
