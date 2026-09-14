"""Competition-scoped discovery for EPL and EPL clubs playing in Europe."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any

from polymarket_bot.errors import BotError, ConfigError
from polymarket_bot.models import utc_now
from polymarket_bot.reporting import (
    _fixture_teams, build_premier_league_report, normalize_team, parse_utc,
    PremierLeagueReport, REPORT_STAKE_CAP_USD,
)


def regular_time_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only explicitly documented 90-minute moneyline/handicap markets.

    In particular, three outcome names alone do not establish settlement scope.
    """
    result = []
    excluded_title = re.compile(r"\b(qualif\w*|advance|extra time|penalt\w*|half|1h|2h|aggregate)\b", re.I)
    regular_time = re.compile(r"(?:first\s+)?90\s*(?:-|–)?\s*minutes?\b", re.I)
    for event in events:
        if excluded_title.search(str(event.get("title", ""))):
            continue
        markets = []
        for market in event.get("markets", []) or []:
            if not isinstance(market, dict) or market.get("sportsMarketType") not in {"moneyline", "spreads"}:
                continue
            if market.get("closed") is True or market.get("active") is False or market.get("acceptingOrders") is False:
                continue
            if excluded_title.search(str(market.get("question", ""))):
                continue
            description = str(market.get("description", ""))
            if not regular_time.search(description):
                continue
            # Reject positive inclusion of extra time, while allowing explicit exclusions.
            if re.search(r"\b(?:including|includes)\s+(?:any\s+)?(?:extra[ -]time|penalties)\b", description, re.I):
                continue
            markets.append(market)
        if markets:
            result.append({**event, "markets": markets})
    return result


def current_epl_teams(events: list[dict[str, Any]], *, now: datetime) -> set[str]:
    """Use dated current-season EPL fixture membership, not the historical team directory."""
    season = now.year if now.month >= 7 else now.year - 1
    start, end = datetime(season, 7, 1, tzinfo=timezone.utc), datetime(season + 1, 7, 1, tzinfo=timezone.utc)
    teams = set()
    for event in events:
        if not str(event.get("slug", "")).startswith("epl-"):
            continue
        if not any(isinstance(m, dict) and m.get("sportsMarketType") == "moneyline" for m in event.get("markets", []) or []):
            continue
        try:
            kickoff = parse_utc(event.get("eventStartTime") or event.get("endDate"), field_name="EPL kickoff")
        except ConfigError:
            continue
        if not start <= kickoff < end:
            continue
        names = _fixture_teams(str(event.get("title", "")))
        if names:
            teams.update(normalize_team(name) for name in names)
    if len(teams) != 20:
        raise ConfigError(f"Current-season EPL fixture coverage identifies {len(teams)} teams, not 20; UCL filtering skipped to avoid an incorrect roster")
    return teams


def build_market_reports(client: Any, *, competitions: tuple[str, ...] = ("epl", "ucl"),
                         providers: dict[str, Any] | None = None, stake_usd: Decimal = REPORT_STAKE_CAP_USD,
                         now: datetime | None = None) -> tuple[list[PremierLeagueReport], list[str]]:
    """Isolate optional competition failures so a healthy section still renders."""
    current = (now or utc_now()).astimezone(timezone.utc)
    reports, warnings = [], []
    if not competitions or any(key not in {"epl", "ucl"} for key in competitions):
        raise ConfigError("Choose epl, ucl, or both")
    epl_data = None
    try:
        epl_data = client.list_epl_events()
    except BotError as error:
        warnings.append(f"EPL fixture discovery unavailable: {error}")
    for competition in competitions:
        try:
            if epl_data is None:
                raise ConfigError("EPL discovery is required to establish the current team scope")
            teams = current_epl_teams(epl_data[1], now=current) if competition == "ucl" else None
            data = epl_data if competition == "epl" else client.list_competition_events("ucl")
            reports.append(build_premier_league_report(client, competition=competition, event_data=data,
                           team_filter=teams, odds_provider=(providers or {}).get(competition),
                           stake_usd=stake_usd, now=current))
        except BotError as error:
            warnings.append(f"{competition.upper()} section unavailable: {error}")
    return reports, warnings
