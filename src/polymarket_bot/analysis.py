from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.analytics_models import (
    AnalyzedBet,
    BetTrade,
    MarketEvent,
    MatchXg,
    TeamForm,
)
from polymarket_bot.reporting import normalize_team


ZERO = Decimal("0")


def _same_team(left: str, right: str) -> bool:
    return normalize_team(left) == normalize_team(right)


def match_xg_fixture(event: MarketEvent, matches: list[MatchXg]) -> MatchXg | None:
    candidates = [
        match
        for match in matches
        if _same_team(event.home_team, match.home_team)
        and _same_team(event.away_team, match.away_team)
        and abs((event.kickoff - match.kickoff).total_seconds()) <= 12 * 3600
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda match: abs(event.kickoff - match.kickoff))


def team_form(
    team: str, *, before: datetime, matches: list[MatchXg], window: int = 5
) -> TeamForm | None:
    observations: list[tuple[datetime, Decimal, Decimal]] = []
    for match in matches:
        if match.kickoff >= before:
            continue
        if _same_team(team, match.home_team):
            observations.append((match.kickoff, match.home_xg, match.away_xg))
        elif _same_team(team, match.away_team):
            observations.append((match.kickoff, match.away_xg, match.home_xg))
    selected = sorted(observations, key=lambda item: item[0], reverse=True)[:window]
    if not selected:
        return None
    count = Decimal(len(selected))
    return TeamForm(
        matches=len(selected),
        xg_for=sum((item[1] for item in selected), ZERO) / count,
        xg_against=sum((item[2] for item in selected), ZERO) / count,
    )


def _actual_role(match: MatchXg) -> str:
    if match.home_goals > match.away_goals:
        return "home"
    if match.home_goals < match.away_goals:
        return "away"
    return "draw"


def _token_side(trade: BetTrade) -> str | None:
    outcome = trade.outcome.casefold()
    if outcome in {"yes", "no"}:
        return outcome.upper()
    if trade.outcome_index == 0:
        return "YES"
    if trade.outcome_index == 1:
        return "NO"
    return None


def analyze_bets(
    trades: list[BetTrade],
    events: dict[str, MarketEvent],
    matches: list[MatchXg],
) -> list[AnalyzedBet]:
    analyzed: list[AnalyzedBet] = []
    for trade in trades:
        if trade.side != "BUY":
            continue
        event = events.get(trade.event_slug)
        role = event.condition_roles.get(trade.condition_id) if event else None
        token_side = _token_side(trade) if role else (trade.outcome.upper() or None)
        match = match_xg_fixture(event, matches) if event else None
        home_form = team_form(event.home_team, before=event.kickoff, matches=matches) if event else None
        away_form = team_form(event.away_team, before=event.kickoff, matches=matches) if event else None

        result = "PENDING"
        hold_pnl: Decimal | None = None
        if match and role and token_side:
            proposition_won = _actual_role(match) == role
            token_won = proposition_won if token_side == "YES" else not proposition_won
            result = "WIN" if token_won else "LOSS"
            hold_pnl = trade.size - trade.usdc_size if token_won else -trade.usdc_size
        analyzed.append(
            AnalyzedBet(
                trade=trade,
                event=event,
                match=match,
                role=role,
                token_side=token_side,
                result=result,
                hold_pnl=hold_pnl,
                home_form=home_form,
                away_form=away_form,
            )
        )
    return analyzed


@dataclass(frozen=True)
class AnalyticsReport:
    generated_at: datetime
    season: int
    user: str
    database_path: str
    trades_stored: int
    closed_positions: int
    realized_pnl: Decimal
    bets: tuple[AnalyzedBet, ...]
    warnings: tuple[str, ...]


def _cell(value: str, width: int) -> str:
    if len(value) > width:
        value = value[: width - 1] + "~"
    return value.ljust(width)


def render_analytics_terminal(report: AnalyticsReport) -> str:
    stake = sum((bet.trade.usdc_size for bet in report.bets), ZERO)
    settled = [bet for bet in report.bets if bet.hold_pnl is not None]
    hold_pnl = sum((bet.hold_pnl or ZERO for bet in settled), ZERO)
    selection_keys = {
        bet.trade.asset or f"{bet.trade.condition_id}:{bet.trade.outcome_index}"
        for bet in report.bets
    }
    settled_selections = {
        bet.trade.asset or f"{bet.trade.condition_id}:{bet.trade.outcome_index}"
        for bet in settled
    }
    winning_selections = {
        bet.trade.asset or f"{bet.trade.condition_id}:{bet.trade.outcome_index}"
        for bet in settled
        if bet.result == "WIN"
    }
    lines = [
        "Premier League bet history and xG analysis",
        (
            f"Season {report.season}/{str(report.season + 1)[-2:]} | "
            f"{report.trades_stored} trades stored | {len(report.bets)} BUY fills across "
            f"{len(selection_keys)} selections | "
            f"${stake:.2f} bought"
        ),
        (
            f"Settled selections: {len(settled_selections)} | "
            f"winning selections: {len(winning_selections)} | "
            f"hold-to-settlement P/L: ${hold_pnl:+.2f}"
        ),
        (
            f"Closed positions: {report.closed_positions} | "
            f"closed-position realized P/L: ${report.realized_pnl:+.2f}"
        ),
        f"Local database: {report.database_path}",
    ]
    lines.extend(f"WARNING: {warning}" for warning in report.warnings)
    if not report.bets:
        lines.append("\nNo EPL BUY trades were found for this season.")
        return "\n".join(lines)

    header = (
        f"{'Date':<10} {_cell('Fixture / market', 38)} {'Sel':<5} "
        f"{'Paid':>6} {'Cost':>8} {'Result':>7} {'HoldPL':>9} {'Post xG':>11}"
    )
    lines.extend(["", header, "-" * len(header)])
    for bet in report.bets:
        fixture = bet.event.title if bet.event else bet.trade.event_slug
        label = (
            f"[{bet.role}] {fixture}"
            if bet.role
            else bet.trade.title or f"{fixture} [unsupported]"
        )
        post_xg = f"{bet.match.home_xg:.2f}-{bet.match.away_xg:.2f}" if bet.match else "n/a"
        lines.append(
            f"{bet.trade.placed_at:%Y-%m-%d} {_cell(label, 38)} "
            f"{_cell(bet.token_side or '?', 5)} {bet.trade.price:>6.3f} "
            f"${bet.trade.usdc_size:>7.2f} "
            f"{bet.result:>7} "
            f"{(f'${bet.hold_pnl:+.2f}' if bet.hold_pnl is not None else 'n/a'):>9} "
            f"{post_xg:>11}"
        )
        if bet.event:
            home_form = (
                f"{bet.home_form.xg_for:.2f}/{bet.home_form.xg_against:.2f} "
                f"({bet.home_form.matches})"
                if bet.home_form
                else "n/a"
            )
            away_form = (
                f"{bet.away_form.xg_for:.2f}/{bet.away_form.xg_against:.2f} "
                f"({bet.away_form.matches})"
                if bet.away_form
                else "n/a"
            )
            lines.append(
                f"             rolling xGF/xGA: {bet.event.home_team} {home_form}; "
                f"{bet.event.away_team} {away_form}"
            )
    lines.extend(
        [
            "",
            "Rolling xG is descriptive context only; figures in parentheses are sample sizes.",
            "HoldPL assumes each BUY fill was held to settlement. Actual realized P/L comes from closed positions.",
            "Understat is a free unofficial source and may change or become unavailable.",
        ]
    )
    return "\n".join(lines)
