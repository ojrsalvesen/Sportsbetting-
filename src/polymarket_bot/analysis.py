from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_bot.analytics_models import (
    AnalyzedBet,
    BetTrade,
    MarketEvent,
    MatchXg,
)
from polymarket_bot.reporting import normalize_team


ZERO = Decimal("0")
SPREAD_PATTERN = re.compile(
    r"^Spread:\s*(?P<team>.+?)\s*\(\s*(?P<line>[+\-−]?\d+(?:\.\d+)?)\s*\)\s*$",
    re.IGNORECASE,
)


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


def xg_matches_for_events(
    events: list[MarketEvent], matches: list[MatchXg]
) -> list[MatchXg]:
    """Return one completed xG match for each represented fixture."""
    selected: dict[str, MatchXg] = {}
    for event in events:
        match = match_xg_fixture(event, matches)
        if match is not None:
            selected[match.match_id] = match
    return sorted(selected.values(), key=lambda match: (match.kickoff, match.match_id))


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


def market_type_for_trade(trade: BetTrade, role: str | None) -> str:
    if role is not None:
        return "Full-time 1X2"
    title = trade.title.casefold()
    if "both teams to score" in title:
        return "Both teams to score"
    if SPREAD_PATTERN.match(trade.title):
        return "Spread"
    if any(word in title for word in ("champion", "championship", "winner")):
        return "Season winner"
    return "Other football market"


def _official_token_won(trade: BetTrade, event: MarketEvent) -> bool | None:
    winner = event.winning_outcomes.get(trade.condition_id)
    if not winner or not trade.outcome:
        return None
    return normalize_team(winner) == normalize_team(trade.outcome)


def _spread_token_won(
    trade: BetTrade, event: MarketEvent, match: MatchXg
) -> bool | None:
    spread = SPREAD_PATTERN.match(trade.title)
    if spread is None:
        return None
    named_team = spread.group("team")
    line = Decimal(spread.group("line").replace("−", "-"))
    # Whole/quarter lines can push or split stakes; they are not binary payouts.
    if abs(line) % 1 != Decimal("0.5"):
        return None
    if _same_team(named_team, event.home_team):
        named_goals, opponent_goals = match.home_goals, match.away_goals
        opponent = event.away_team
    elif _same_team(named_team, event.away_team):
        named_goals, opponent_goals = match.away_goals, match.home_goals
        opponent = event.home_team
    else:
        return None

    if _same_team(trade.outcome, named_team):
        selected_named_team = True
    elif _same_team(trade.outcome, opponent):
        selected_named_team = False
    else:
        return None

    adjusted_named_goals = Decimal(named_goals) + line
    if adjusted_named_goals == opponent_goals:
        return None
    named_team_covered = adjusted_named_goals > opponent_goals
    return named_team_covered == selected_named_team


def _score_based_token_won(
    trade: BetTrade,
    event: MarketEvent,
    match: MatchXg,
    role: str | None,
    market_type: str,
) -> bool | None:
    if re.search(r"\b(half|period|extra\s*time|penalt\w*|1h|2h|ht|qualif\w*|advance|aggregate)\b", trade.title, re.I):
        return None
    if role is not None:
        token_side = _token_side(trade)
        if token_side is None:
            return None
        proposition_won = _actual_role(match) == role
        return proposition_won if token_side == "YES" else not proposition_won
    if market_type == "Both teams to score":
        if not re.fullmatch(r".+\s+vs\.?\s+.+:\s*Both Teams to Score\??", trade.title, re.I):
            return None
        token_side = _token_side(trade)
        if token_side is None:
            return None
        proposition_won = match.home_goals > 0 and match.away_goals > 0
        return proposition_won if token_side == "YES" else not proposition_won
    if market_type == "Spread":
        return _spread_token_won(trade, event, match)
    return None


def _directional_teams(
    trade: BetTrade,
    event: MarketEvent,
    role: str | None,
    market_type: str,
) -> tuple[str, str, bool] | None:
    if role in {"home", "away"}:
        proposition_is_home = role == "home"
        token_side = _token_side(trade)
        if token_side not in {"YES", "NO"}:
            return None
        direction_is_home = (
            proposition_is_home if token_side == "YES" else not proposition_is_home
        )
    elif market_type == "Spread":
        if _same_team(trade.outcome, event.home_team):
            direction_is_home = True
        elif _same_team(trade.outcome, event.away_team):
            direction_is_home = False
        else:
            return None
    else:
        return None

    if direction_is_home:
        return event.home_team, event.away_team, True
    return event.away_team, event.home_team, False


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
        market_type = market_type_for_trade(trade, role)
        direction = (
            _directional_teams(trade, event, role, market_type) if event else None
        )
        match = match_xg_fixture(event, matches) if event else None
        result = "PENDING"
        resolution_source: str | None = None
        hold_pnl: Decimal | None = None
        token_won = _official_token_won(trade, event) if event else None
        if token_won is not None:
            resolution_source = "Polymarket"
        if token_won is None and event and match:
            token_won = _score_based_token_won(
                trade, event, match, role, market_type
            )
            if token_won is not None:
                resolution_source = "Final-score fallback"
        if token_won is not None:
            result = "WIN" if token_won else "LOSS"
            hold_pnl = trade.size - trade.usdc_size if token_won else -trade.usdc_size
        analyzed.append(
            AnalyzedBet(
                trade=trade,
                event=event,
                match=match,
                role=role,
                token_side=token_side,
                market_type=market_type,
                direction_team=direction[0] if direction else None,
                direction_opponent=direction[1] if direction else None,
                direction_is_home=direction[2] if direction else None,
                result=result,
                resolution_source=resolution_source,
                hold_pnl=hold_pnl,
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
        "Premier League and Champions League bet history and xG analysis",
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
        lines.append("\nNo EPL or UCL BUY trades were found for this season.")
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
    lines.extend(
        [
            "",
            "Post xG is shown only for completed fixtures represented in the betting ledger.",
            "Resolved outcomes come from Polymarket; final scores provide a fallback for full-match 1X2, BTTS, and half-goal spreads.",
            "HoldPL assumes each BUY fill was held to settlement. Actual realized P/L comes from closed positions.",
            "Understat is a free unofficial source and may change or become unavailable.",
        ]
    )
    return "\n".join(lines)
