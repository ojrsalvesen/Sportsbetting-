from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

import httpx

from polymarket_bot.errors import ApiError, ConfigError, MarketResolutionError
from polymarket_bot.market_data import _json_list, _market_info, PolymarketPublicClient
from polymarket_bot.models import MarketInfo, OrderBook, decimal_value, utc_now


ZERO = Decimal("0")
REPORT_STAKE_CAP_USD = Decimal("5.00")
DEPTH_WINDOWS = (Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))
THE_ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
FREE_TIER_PINNACLE_REFRESH_SECONDS = 5400.0  # 90 minutes => at most 496 calls/31 days.


def parse_utc(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be an ISO-8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as error:
        raise ConfigError(f"{field_name} must be an ISO-8601 timestamp") from error
    if result.tzinfo is None:
        raise ConfigError(f"{field_name} must include a timezone")
    return result.astimezone(timezone.utc)


def normalize_team(value: str) -> str:
    text = value.casefold().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", text)
    tokens = [token for token in tokens if token not in {"afc", "fc", "football", "club"}]
    normalized = " ".join(tokens)
    aliases = {
        "man city": "manchester city",
        "man utd": "manchester united",
        "man united": "manchester united",
        "nottm forest": "nottingham forest",
        "spurs": "tottenham hotspur",
        "tottenham": "tottenham hotspur",
        "brighton": "brighton and hove albion",
        "bournemouth": "bournemouth",
        "coventry": "coventry city",
        "hull": "hull city",
        "ipswich": "ipswich town",
        "leeds": "leeds united",
        "newcastle": "newcastle united",
        "west ham": "west ham united",
        "wolves": "wolverhampton wanderers",
    }
    return aliases.get(normalized, normalized)


@dataclass(frozen=True)
class FixtureOutcome:
    role: str
    label: str
    market: MarketInfo
    no_token_id: str

    def token_ids(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return (("YES", self.market.token_id), ("NO", self.no_token_id))


@dataclass(frozen=True)
class Fixture:
    event_id: str
    title: str
    slug: str
    starts_at: datetime
    home_team: str
    away_team: str
    outcomes: tuple[FixtureOutcome, ...]

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


def _fixture_teams(title: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return None
    return parts[0].strip(), parts[1].strip()


def moneyline_fixture_from_event(
    raw: dict[str, Any], *, now: datetime | None = None
) -> Fixture | None:
    """Convert only a full-time three-way EPL moneyline event into a fixture."""
    markets = raw.get("markets")
    if not isinstance(markets, list):
        return None
    moneyline = [
        market
        for market in markets
        if isinstance(market, dict) and market.get("sportsMarketType") == "moneyline"
    ]
    if len(moneyline) != 3:
        return None

    title = str(raw.get("title", "")).strip()
    teams = _fixture_teams(title)
    if teams is None:
        return None
    home_team, away_team = teams
    start_value = raw.get("eventStartTime") or raw.get("endDate")
    try:
        starts_at = parse_utc(start_value, field_name="event start")
    except ConfigError:
        return None
    if starts_at < (now or utc_now()).astimezone(timezone.utc):
        return None

    parsed: dict[str, FixtureOutcome] = {}
    home_key = normalize_team(home_team)
    away_key = normalize_team(away_team)
    for market in moneyline:
        outcomes = [str(value) for value in _json_list(market.get("outcomes"), name="outcomes")]
        token_ids = [
            str(value) for value in _json_list(market.get("clobTokenIds"), name="token IDs")
        ]
        yes_indexes = [index for index, value in enumerate(outcomes) if value.casefold() == "yes"]
        no_indexes = [index for index, value in enumerate(outcomes) if value.casefold() == "no"]
        if (
            len(outcomes) != len(token_ids)
            or len(yes_indexes) != 1
            or len(no_indexes) != 1
        ):
            raise MarketResolutionError(f"Malformed moneyline market in event '{title}'")

        group_title = str(market.get("groupItemTitle", "")).strip()
        question = str(market.get("question", "")).casefold()
        group_key = normalize_team(group_title)
        if "draw" in group_title.casefold() or "end in a draw" in question:
            role, label = "draw", "Draw"
        elif group_key == home_key:
            role, label = "home", home_team
        elif group_key == away_key:
            role, label = "away", away_team
        else:
            raise MarketResolutionError(
                f"Could not map moneyline outcome '{group_title}' in event '{title}'"
            )
        if role in parsed:
            raise MarketResolutionError(f"Duplicate {role} moneyline in event '{title}'")
        index = yes_indexes[0]
        parsed[role] = FixtureOutcome(
            role=role,
            label=label,
            market=_market_info(market, token_ids[index]),
            no_token_id=token_ids[no_indexes[0]],
        )

    if set(parsed) != {"home", "draw", "away"}:
        raise MarketResolutionError(f"Incomplete three-way moneyline in event '{title}'")
    return Fixture(
        event_id=str(raw.get("id", "")),
        title=title,
        slug=str(raw.get("slug", "")),
        starts_at=starts_at,
        home_team=home_team,
        away_team=away_team,
        outcomes=tuple(parsed[role] for role in ("home", "draw", "away")),
    )


def discover_fixtures(
    events: list[dict[str, Any]], *, now: datetime | None = None
) -> tuple[list[Fixture], list[str]]:
    current = now or utc_now()
    fixtures: list[Fixture] = []
    warnings: list[str] = []
    for event in events:
        try:
            fixture = moneyline_fixture_from_event(event, now=current)
        except MarketResolutionError as error:
            warnings.append(str(error))
            continue
        if fixture is not None:
            fixtures.append(fixture)
    unique = {fixture.event_id: fixture for fixture in fixtures}
    return sorted(unique.values(), key=lambda item: (item.starts_at, item.title)), warnings


@dataclass(frozen=True)
class OddsQuote:
    home_team: str
    away_team: str
    starts_at: datetime
    captured_at: datetime
    home_odds: Decimal
    draw_odds: Decimal
    away_odds: Decimal
    def __post_init__(self) -> None:
        if min(self.home_odds, self.draw_odds, self.away_odds) <= Decimal("1"):
            raise ConfigError("Decimal odds must all be greater than 1")

    def fair_probability(self, role: str) -> Decimal:
        raw = {
            "home": Decimal("1") / self.home_odds,
            "draw": Decimal("1") / self.draw_odds,
            "away": Decimal("1") / self.away_odds,
        }
        return raw[role] / sum(raw.values(), ZERO)


class OddsProvider(Protocol):
    @property
    def description(self) -> str: ...

    def quote_for(self, fixture: Fixture) -> OddsQuote | None: ...


class NoOddsProvider:
    description = "not configured"

    def quote_for(self, fixture: Fixture) -> OddsQuote | None:
        return None


def _matching_quote(
    quotes: tuple[OddsQuote, ...],
    fixture: Fixture,
    *,
    kickoff_tolerance_hours: int,
) -> OddsQuote | None:
    home_key = normalize_team(fixture.home_team)
    away_key = normalize_team(fixture.away_team)
    candidates = [
        quote
        for quote in quotes
        if normalize_team(quote.home_team) == home_key
        and normalize_team(quote.away_team) == away_key
        and abs((quote.starts_at - fixture.starts_at).total_seconds())
        <= kickoff_tolerance_hours * 3600
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda quote: abs(quote.starts_at - fixture.starts_at))


class TheOddsApiPinnacleProvider:
    """Fetch Pinnacle EPL 1X2 odds through The Odds API's documented endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        refresh_seconds: float = FREE_TIER_PINNACLE_REFRESH_SECONDS,
        kickoff_tolerance_hours: int = 12,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip():
            raise ConfigError("The Odds API key is required")
        if refresh_seconds < FREE_TIER_PINNACLE_REFRESH_SECONDS:
            raise ConfigError(
                "Free-only Pinnacle refresh must be at least 5400 seconds (90 minutes)"
            )
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self.refresh_seconds = refresh_seconds
        self.kickoff_tolerance_hours = kickoff_tolerance_hours
        self._client = client
        self._last_attempt_monotonic: float | None = None
        self.quotes: tuple[OddsQuote, ...] = ()
        self.credits_remaining: int | None = None
        self.credits_used: int | None = None
        self.last_request_cost: int | None = None

    def __repr__(self) -> str:
        return (
            "TheOddsApiPinnacleProvider(api_key=<redacted>, "
            f"quotes={len(self.quotes)}, refresh_seconds={self.refresh_seconds})"
        )

    @property
    def description(self) -> str:
        quota = (
            f", {self.credits_remaining} credits remaining"
            if self.credits_remaining is not None
            else ""
        )
        latest = (
            f", updated {max(quote.captured_at for quote in self.quotes):%H:%M} UTC"
            if self.quotes
            else ""
        )
        return f"The Odds API / Pinnacle ({len(self.quotes)} fixtures{latest}{quota})"

    @staticmethod
    def _header_int(response: httpx.Response, name: str) -> int | None:
        raw = response.headers.get(name)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def _get(self) -> httpx.Response:
        params = {
            "apiKey": self._api_key,
            "bookmakers": "pinnacle",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        try:
            if self._client is not None:
                return self._client.get(THE_ODDS_API_URL, params=params)
            with httpx.Client(timeout=self._timeout_seconds, follow_redirects=True) as client:
                return client.get(THE_ODDS_API_URL, params=params)
        except httpx.HTTPError as error:
            raise ApiError(
                f"The Odds API request failed ({type(error).__name__}); API key was not logged"
            ) from None

    def refresh_if_due(self, *, force: bool = False) -> bool:
        current = time.monotonic()
        if (
            not force
            and self._last_attempt_monotonic is not None
            and current - self._last_attempt_monotonic < self.refresh_seconds
        ):
            return False
        self._last_attempt_monotonic = current
        response = self._get()
        self.credits_remaining = self._header_int(response, "x-requests-remaining")
        self.credits_used = self._header_int(response, "x-requests-used")
        self.last_request_cost = self._header_int(response, "x-requests-last")
        if response.status_code >= 400:
            message = "request rejected"
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("message"):
                    message = str(payload["message"])
            except ValueError:
                pass
            message = message.replace(self._api_key, "<redacted>")
            raise ApiError(
                f"The Odds API returned HTTP {response.status_code}: {message}"
            )
        try:
            data = response.json()
        except ValueError:
            raise ApiError("The Odds API returned invalid JSON") from None
        if not isinstance(data, list):
            raise ApiError("The Odds API returned an unexpected response")

        quotes: list[OddsQuote] = []
        for event in data:
            if not isinstance(event, dict):
                continue
            home_team = str(event.get("home_team", "")).strip()
            away_team = str(event.get("away_team", "")).strip()
            try:
                starts_at = parse_utc(event.get("commence_time"), field_name="commence_time")
            except ConfigError:
                continue
            bookmakers = event.get("bookmakers")
            if not home_team or not away_team or not isinstance(bookmakers, list):
                continue
            pinnacle = next(
                (
                    book
                    for book in bookmakers
                    if isinstance(book, dict)
                    and str(book.get("key", "")).casefold() == "pinnacle"
                ),
                None,
            )
            if pinnacle is None or not isinstance(pinnacle.get("markets"), list):
                continue
            market = next(
                (
                    item
                    for item in pinnacle["markets"]
                    if isinstance(item, dict)
                    and str(item.get("key", "")).casefold() == "h2h"
                ),
                None,
            )
            if market is None or not isinstance(market.get("outcomes"), list):
                continue
            prices: dict[str, Decimal] = {}
            for raw_outcome in market["outcomes"]:
                if not isinstance(raw_outcome, dict):
                    continue
                name = str(raw_outcome.get("name", "")).strip()
                if name.casefold() == "draw":
                    role = "draw"
                elif normalize_team(name) == normalize_team(home_team):
                    role = "home"
                elif normalize_team(name) == normalize_team(away_team):
                    role = "away"
                else:
                    continue
                try:
                    prices[role] = decimal_value(
                        raw_outcome.get("price"), field_name=f"{role} odds"
                    )
                except ConfigError:
                    continue
            if set(prices) != {"home", "draw", "away"}:
                continue
            captured_value = market.get("last_update") or pinnacle.get("last_update")
            try:
                captured_at = parse_utc(captured_value, field_name="last_update")
                quotes.append(
                    OddsQuote(
                        home_team=home_team,
                        away_team=away_team,
                        starts_at=starts_at,
                        captured_at=captured_at,
                        home_odds=prices["home"],
                        draw_odds=prices["draw"],
                        away_odds=prices["away"],
                    )
                )
            except ConfigError:
                continue
        self.quotes = tuple(quotes)
        return True

    def quote_for(self, fixture: Fixture) -> OddsQuote | None:
        return _matching_quote(
            self.quotes,
            fixture,
            kickoff_tolerance_hours=self.kickoff_tolerance_hours,
        )


@dataclass(frozen=True)
class BookMetrics:
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    bid_depth_usd: dict[str, Decimal]
    ask_depth_usd: dict[str, Decimal]
    fillable_spend_usd: Decimal
    buy_vwap: Decimal | None


def calculate_book_metrics(book: OrderBook, *, target_spend_usd: Decimal) -> BookMetrics:
    if target_spend_usd <= ZERO or target_spend_usd > REPORT_STAKE_CAP_USD:
        raise ConfigError(f"Report stake must be above $0 and at most ${REPORT_STAKE_CAP_USD}")
    bids = sorted(book.bids, key=lambda level: level.price, reverse=True)
    asks = sorted(book.asks, key=lambda level: level.price)
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None

    def window_key(window: Decimal) -> str:
        return f"{int(window * 100)}c"

    bid_depth = {
        window_key(window): sum(
            (level.price * level.size for level in bids if level.price >= best_bid - window),
            ZERO,
        )
        if best_bid is not None
        else ZERO
        for window in DEPTH_WINDOWS
    }
    ask_depth = {
        window_key(window): sum(
            (level.price * level.size for level in asks if level.price <= best_ask + window),
            ZERO,
        )
        if best_ask is not None
        else ZERO
        for window in DEPTH_WINDOWS
    }

    spent = ZERO
    shares = ZERO
    for level in asks:
        remaining = target_spend_usd - spent
        if remaining <= ZERO:
            break
        level_spend = level.price * level.size
        take_spend = min(remaining, level_spend)
        spent += take_spend
        shares += take_spend / level.price
    vwap = spent / shares if shares > ZERO else None

    return BookMetrics(
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        bid_depth_usd=bid_depth,
        ask_depth_usd=ask_depth,
        fillable_spend_usd=spent,
        buy_vwap=vwap,
    )


@dataclass(frozen=True)
class OutcomeReport:
    outcome: FixtureOutcome
    token_side: str
    token_id: str
    metrics: BookMetrics | None
    error: str | None
    pinnacle_fair_probability: Decimal | None
    price_gap: Decimal | None


@dataclass(frozen=True)
class FixtureReport:
    fixture: Fixture
    outcomes: tuple[OutcomeReport, ...]


@dataclass(frozen=True)
class PremierLeagueReport:
    generated_at: datetime
    series_id: str
    stake_usd: Decimal
    odds_source: str
    fixtures: tuple[FixtureReport, ...]
    warnings: tuple[str, ...]


def build_premier_league_report(
    client: PolymarketPublicClient,
    *,
    odds_provider: OddsProvider | None = None,
    stake_usd: Decimal = REPORT_STAKE_CAP_USD,
    now: datetime | None = None,
) -> PremierLeagueReport:
    generated_at = (now or utc_now()).astimezone(timezone.utc)
    if stake_usd <= ZERO or stake_usd > REPORT_STAKE_CAP_USD:
        raise ConfigError(f"Report stake must be above $0 and at most ${REPORT_STAKE_CAP_USD}")
    provider = odds_provider or NoOddsProvider()
    series_id, raw_events = client.list_epl_events()
    fixtures, warnings = discover_fixtures(raw_events, now=generated_at)
    token_ids = [
        token_id
        for fixture in fixtures
        for outcome in fixture.outcomes
        for _, token_id in outcome.token_ids()
    ]
    book_errors: dict[str, str] = {}
    try:
        books = client.get_order_books(token_ids) if token_ids else {}
    except ApiError as error:
        books = {}
        warnings.append(f"Batch order-book fetch failed; retrying books individually: {error}")
        for token_id in token_ids:
            try:
                books[token_id] = client.get_order_book(token_id)
            except ApiError as book_error:
                book_errors[token_id] = str(book_error)

    fixture_reports: list[FixtureReport] = []
    fixtures_without_odds = 0
    for fixture in fixtures:
        quote = provider.quote_for(fixture)
        if quote is None:
            fixtures_without_odds += 1
        outcome_reports: list[OutcomeReport] = []
        for outcome in fixture.outcomes:
            yes_fair = quote.fair_probability(outcome.role) if quote else None
            for token_side, token_id in outcome.token_ids():
                book = books.get(token_id)
                error: str | None = None
                metrics: BookMetrics | None = None
                if book is None:
                    error = book_errors.get(token_id, "order book unavailable")
                elif book.condition_id != outcome.market.condition_id:
                    error = "order book condition ID mismatch"
                else:
                    metrics = calculate_book_metrics(book, target_spend_usd=stake_usd)
                fair = (
                    yes_fair
                    if token_side == "YES"
                    else Decimal("1") - yes_fair
                    if yes_fair is not None
                    else None
                )
                execution_price = (
                    metrics.buy_vwap
                    if metrics and metrics.fillable_spend_usd >= stake_usd
                    else None
                )
                outcome_reports.append(
                    OutcomeReport(
                        outcome=outcome,
                        token_side=token_side,
                        token_id=token_id,
                        metrics=metrics,
                        error=error,
                        pinnacle_fair_probability=fair,
                        price_gap=(
                            fair - execution_price
                            if fair is not None and execution_price is not None
                            else None
                        ),
                    )
                )
        fixture_reports.append(FixtureReport(fixture=fixture, outcomes=tuple(outcome_reports)))
    if not fixtures:
        warnings.append("No upcoming full-time EPL moneyline fixtures were returned by Polymarket")
    if isinstance(provider, NoOddsProvider):
        warnings.append(
            "Pinnacle comparison is disabled; run with --pinnacle-api and a free API key"
        )
    elif fixtures_without_odds:
        warnings.append(
            f"External odds source had no matching quote for {fixtures_without_odds} of "
            f"{len(fixtures)} fixtures"
        )
    return PremierLeagueReport(
        generated_at=generated_at,
        series_id=series_id,
        stake_usd=stake_usd,
        odds_source=provider.description,
        fixtures=tuple(fixture_reports),
        warnings=tuple(warnings),
    )


def _terminal_cell(value: str, width: int) -> str:
    if len(value) > width:
        value = value[: width - 1] + "~"
    return value.ljust(width)


def render_terminal(report: PremierLeagueReport) -> str:
    token_book_count = sum(len(fixture.outcomes) for fixture in report.fixtures)
    healthy_count = sum(
        outcome.error is None
        for fixture in report.fixtures
        for outcome in fixture.outcomes
    )
    lines = [
        "Premier League market monitor",
        (
            f"Updated {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')} UTC | "
            f"{len(report.fixtures)} fixtures | {healthy_count}/{token_book_count} books healthy | "
            f"Pinnacle: {report.odds_source}"
        ),
    ]
    lines.extend(f"WARNING: {warning}" for warning in report.warnings)
    header = (
        f"{_terminal_cell('Outcome', 27)} "
        f"{'Tok':<3} {'Bid':>6} {'Ask':>6} {'Spr':>6} "
        f"{'Bid$2c':>10} {'Ask$2c':>10} {'BuyVWAP':>7} "
        f"{'PinFair':>8} {'Gap':>8}"
    )
    divider = "-" * len(header)
    for fixture_report in report.fixtures:
        fixture = fixture_report.fixture
        lines.extend(
            [
                "",
                f"{fixture.starts_at.strftime('%Y-%m-%d %H:%M')} UTC  {fixture.title}",
                fixture.url,
                header,
                divider,
            ]
        )
        for outcome_report in fixture_report.outcomes:
            metrics = outcome_report.metrics
            fair = outcome_report.pinnacle_fair_probability
            gap = outcome_report.price_gap

            def price(value: Decimal | None) -> str:
                return f"{value:.3f}" if value is not None else "n/a"

            def usd(value: Decimal | None) -> str:
                return f"${value:,.0f}" if value is not None else "n/a"

            lines.append(
                f"{_terminal_cell(outcome_report.outcome.label, 27)} "
                f"{outcome_report.token_side:<3} "
                f"{price(metrics.best_bid if metrics else None):>6} "
                f"{price(metrics.best_ask if metrics else None):>6} "
                f"{price(metrics.spread if metrics else None):>6} "
                f"{usd(metrics.bid_depth_usd['2c'] if metrics else None):>10} "
                f"{usd(metrics.ask_depth_usd['2c'] if metrics else None):>10} "
                f"{price(metrics.buy_vwap if metrics and metrics.fillable_spend_usd >= report.stake_usd else None):>7} "
                f"{(f'{fair:.2%}' if fair is not None else 'n/a'):>8} "
                f"{(f'{gap:+.2%}' if gap is not None else 'n/a'):>8}"
            )
            if outcome_report.error:
                lines.append(f"  BOOK ERROR: {outcome_report.error}")
    lines.extend(
        [
            "",
            "Bid$2c/Ask$2c = visible USD depth within 2 cents of the best price.",
            f"BuyVWAP walks visible asks for the configured ${report.stake_usd:.2f} purchase.",
            "PinFair is margin-normalized. Gap = PinFair - BuyVWAP; it is not a profit forecast.",
        ]
    )
    return "\n".join(lines)
