from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

import httpx

from polymarket_bot.errors import ApiError, ConfigError, MarketResolutionError
from polymarket_bot.market_data import _json_list, _market_info, PolymarketPublicClient
from polymarket_bot.models import MarketInfo, OrderBook, decimal_value, utc_now
from polymarket_bot.odds_audit import OddsRequestBudget, save_quote_snapshot


ZERO = Decimal("0")
REPORT_STAKE_CAP_USD = Decimal("5.00")
DEPTH_WINDOWS = (Decimal("0.01"), Decimal("0.02"), Decimal("0.05"))
OUTCOME_COLUMN_WIDTH = 32
THE_ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
SPORT_KEYS = {"epl": "soccer_epl", "ucl": "soccer_uefa_champs_league"}
EPL_FIXTURE_WINDOW = timedelta(days=7)
# The bulk h2h/spreads request costs two credits and exact alternate handicaps can
# add one request per fixture. Twenty hours keeps the 12-credit worst case below
# 500 credits over a 31-day month in continuously running watch mode.
FREE_TIER_PINNACLE_REFRESH_SECONDS = 72000.0


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
    text = unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode().replace("&", " and ")
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
        "psg": "paris saint germain",
        "paris saint germain": "paris saint germain",
        "bayern munich": "bayern munchen",
        "inter milan": "internazionale milano",
        "inter": "internazionale milano",
        "sporting cp": "sporting lisbon",
        "sporting clube de portugal": "sporting lisbon",
        "bv borussia 09 dortmund": "borussia dortmund",
        "brugge kv": "brugge",
        "atletico de madrid": "atletico madrid",
        "ssc napoli": "napoli",
        "sabah fk": "sabah",
        "sabah baku": "sabah",
        "real madrid cf": "real madrid",
        "villarreal cf": "villarreal",
        "real betis balompie": "real betis",
        "lille osc": "lille",
        "sl benfica": "benfica",
    }
    return aliases.get(normalized, normalized)


@dataclass(frozen=True)
class FixtureOutcome:
    role: str
    label: str
    market: MarketInfo
    no_token_id: str
    reference_probability: Decimal | None

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


@dataclass(frozen=True)
class HandicapOutcome:
    role: str
    team: str
    line: Decimal
    market: MarketInfo

    @property
    def label(self) -> str:
        return f"{self.team} {self.line:+g}"


@dataclass(frozen=True)
class HandicapMarket:
    event_id: str
    title: str
    slug: str
    starts_at: datetime
    volume: Decimal
    liquidity: Decimal
    outcomes: tuple[HandicapOutcome, HandicapOutcome]

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


def _fixture_teams(title: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+vs\.?\s+", title, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return None
    return parts[0].strip(), parts[1].strip()


def _yes_reference_probability(market: dict[str, Any], yes_index: int) -> Decimal | None:
    try:
        prices = _json_list(market.get("outcomePrices"), name="outcome prices")
        candidate = decimal_value(prices[yes_index], field_name="YES reference price")
        if ZERO <= candidate <= Decimal("1"):
            return candidate
    except (ConfigError, IndexError, MarketResolutionError):
        pass
    for field_name in ("lastTradePrice", "bestAsk", "bestBid"):
        try:
            candidate = decimal_value(
                market.get(field_name), field_name=f"{field_name} reference price"
            )
        except ConfigError:
            continue
        if ZERO <= candidate <= Decimal("1"):
            return candidate
    return None


def moneyline_fixture_from_event(
    raw: dict[str, Any], *, now: datetime | None = None
) -> Fixture | None:
    """Convert a full-time three-way football moneyline event into a fixture."""
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
    if starts_at <= (now or utc_now()).astimezone(timezone.utc):
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
            reference_probability=_yes_reference_probability(market, index),
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


def select_upcoming_week(
    fixtures: list[Fixture], *, now: datetime | None = None
) -> list[Fixture]:
    """Return every kickoff after now and at most seven days away, in order."""
    current = (now or utc_now()).astimezone(timezone.utc)
    cutoff = current + EPL_FIXTURE_WINDOW
    return sorted(
        (fixture for fixture in fixtures if current < fixture.starts_at <= cutoff),
        key=lambda item: (item.starts_at, item.title),
    )


def _nonnegative_market_value(value: Any, *, field_name: str) -> Decimal:
    if value is None or value == "":
        return ZERO
    parsed = decimal_value(value, field_name=field_name)
    if parsed < ZERO:
        raise ConfigError(f"{field_name} must not be negative")
    return parsed


def _handicap_market_from_raw(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    starts_at: datetime,
    home_team: str,
    away_team: str,
) -> HandicapMarket:
    outcomes = [str(value) for value in _json_list(market.get("outcomes"), name="outcomes")]
    token_ids = [
        str(value) for value in _json_list(market.get("clobTokenIds"), name="token IDs")
    ]
    if len(outcomes) != 2 or len(token_ids) != 2 or not all(token_ids):
        raise MarketResolutionError("Handicap market must contain exactly two tokenized outcomes")
    line = decimal_value(market.get("line"), field_name="handicap line")
    if abs(line) % 1 != Decimal("0.5"):
        raise MarketResolutionError("Only half-goal handicaps have a comparable binary payout")

    home_key = normalize_team(home_team)
    away_key = normalize_team(away_team)
    parsed: list[HandicapOutcome] = []
    for index, (team, token_id) in enumerate(zip(outcomes, token_ids, strict=True)):
        team_key = normalize_team(team)
        if team_key == home_key:
            role = "home"
        elif team_key == away_key:
            role = "away"
        else:
            raise MarketResolutionError(f"Could not map handicap outcome '{team}'")
        parsed.append(
            HandicapOutcome(
                role=role,
                team=home_team if role == "home" else away_team,
                line=line if index == 0 else -line,
                market=_market_info(market, token_id),
            )
        )
    if {outcome.role for outcome in parsed} != {"home", "away"}:
        raise MarketResolutionError("Handicap market must contain both fixture teams")
    parsed.sort(key=lambda outcome: 0 if outcome.role == "home" else 1)
    return HandicapMarket(
        event_id=str(event.get("id", "")),
        title=str(market.get("question", "")).strip() or "Full-match handicap",
        slug=str(event.get("slug", "")),
        starts_at=starts_at,
        volume=_nonnegative_market_value(market.get("volume"), field_name="handicap volume"),
        liquidity=_nonnegative_market_value(
            market.get("liquidity"), field_name="handicap liquidity"
        ),
        outcomes=(parsed[0], parsed[1]),
    )


def discover_popular_handicaps(
    events: list[dict[str, Any]],
    fixtures: list[Fixture],
    *,
    preferred_lines: dict[str, tuple[Decimal, Decimal]] | None = None,
    favorite_roles: dict[str, str] | None = None,
) -> tuple[dict[str, HandicapMarket], list[str]]:
    """Select a negative handicap for the favourite, preferring Pinnacle's main line."""
    candidates: dict[str, list[HandicapMarket]] = {
        fixture.event_id: [] for fixture in fixtures
    }
    warnings: list[str] = []
    for event in events:
        markets = event.get("markets")
        if not isinstance(markets, list) or not any(
            isinstance(market, dict)
            and str(market.get("sportsMarketType", "")).casefold() == "spreads"
            for market in markets
        ):
            continue
        title = re.sub(
            r"\s+-\s+More Markets\s*$", "", str(event.get("title", "")).strip(),
            flags=re.IGNORECASE,
        )
        teams = _fixture_teams(title)
        if teams is None:
            continue
        start_value = event.get("eventStartTime") or event.get("endDate")
        try:
            starts_at = parse_utc(start_value, field_name="event start")
        except ConfigError:
            continue
        home_key, away_key = (normalize_team(team) for team in teams)
        matching = [
            fixture
            for fixture in fixtures
            if normalize_team(fixture.home_team) == home_key
            and normalize_team(fixture.away_team) == away_key
            and abs((fixture.starts_at - starts_at).total_seconds()) <= 12 * 3600
        ]
        if len(matching) != 1:
            continue
        fixture = matching[0]
        for market in markets:
            if (
                not isinstance(market, dict)
                or str(market.get("sportsMarketType", "")).casefold() != "spreads"
            ):
                continue
            try:
                candidate = _handicap_market_from_raw(
                    event,
                    market,
                    starts_at=starts_at,
                    home_team=fixture.home_team,
                    away_team=fixture.away_team,
                )
            except (ConfigError, MarketResolutionError) as error:
                warnings.append(
                    f"Ignored malformed handicap in '{fixture.title}': {error}"
                )
                continue
            candidates[fixture.event_id].append(candidate)

    selected: dict[str, HandicapMarket] = {}
    for fixture in fixtures:
        available = candidates[fixture.event_id]
        favorite_role = (favorite_roles or {}).get(fixture.event_id)
        eligible = [
            market
            for market in available
            if favorite_role is None
            or any(
                outcome.role == favorite_role and outcome.line < ZERO
                for outcome in market.outcomes
            )
        ]
        if eligible:
            preferred = (preferred_lines or {}).get(fixture.event_id)
            exact = [
                market
                for market in eligible
                if preferred is not None
                and tuple(outcome.line for outcome in market.outcomes) == preferred
            ]
            selected[fixture.event_id] = sorted(
                exact or eligible,
                key=lambda market: (-market.volume, -market.liquidity, market.title),
            )[0]
    return selected, warnings


@dataclass(frozen=True)
class HandicapQuote:
    home_line: Decimal
    away_line: Decimal
    home_odds: Decimal
    away_odds: Decimal
    captured_at: datetime

    def __post_init__(self) -> None:
        if min(self.home_odds, self.away_odds) <= Decimal("1"):
            raise ConfigError("Handicap decimal odds must both be greater than 1")
        if self.home_line != -self.away_line:
            raise ConfigError("Home and away handicap lines must be opposites")

    def fair_probability(self, role: str, line: Decimal) -> Decimal | None:
        if role not in {"home", "away"}:
            raise ConfigError("Handicap role must be home or away")
        expected_line = self.home_line if role == "home" else self.away_line
        if line != expected_line:
            return None
        if abs(line) % 1 != Decimal("0.5"):
            return None
        raw_home = Decimal("1") / self.home_odds
        raw_away = Decimal("1") / self.away_odds
        raw = raw_home if role == "home" else raw_away
        return raw / (raw_home + raw_away)


@dataclass(frozen=True)
class OddsQuote:
    home_team: str
    away_team: str
    starts_at: datetime
    captured_at: datetime
    home_odds: Decimal
    draw_odds: Decimal
    away_odds: Decimal
    handicap: HandicapQuote | None = None
    source_event_id: str | None = None
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


def favorite_team_role(fixture: Fixture, quote: OddsQuote | None) -> str | None:
    """Identify the shorter-priced team, excluding the draw outcome."""
    if quote is not None and quote.home_odds != quote.away_odds:
        return "home" if quote.home_odds < quote.away_odds else "away"
    team_outcomes = {
        outcome.role: outcome.reference_probability
        for outcome in fixture.outcomes
        if outcome.role in {"home", "away"}
    }
    home_probability = team_outcomes.get("home")
    away_probability = team_outcomes.get("away")
    if (
        home_probability is None
        or away_probability is None
        or home_probability == away_probability
    ):
        return None
    return "home" if home_probability > away_probability else "away"


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
    if len(candidates) != 1:
        return None
    return candidates[0]


class TheOddsApiPinnacleProvider:
    """Fetch one competition's Pinnacle 90-minute prices through The Odds API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 20.0,
        refresh_seconds: float = FREE_TIER_PINNACLE_REFRESH_SECONDS,
        kickoff_tolerance_hours: int = 12,
        client: httpx.Client | None = None,
        sport_key: str = "soccer_epl",
        budget: OddsRequestBudget | None = None,
        snapshot_path: str | Path | None = None,
        max_alternate_requests: int = 10,
    ):
        if not api_key.strip():
            raise ConfigError("The Odds API key is required")
        if refresh_seconds < FREE_TIER_PINNACLE_REFRESH_SECONDS:
            raise ConfigError(
                "Free-only Pinnacle refresh must be at least 72000 seconds (20 hours)"
            )
        self._api_key = api_key.strip()
        if sport_key not in SPORT_KEYS.values():
            raise ConfigError("Unsupported Pinnacle football competition")
        self.sport_key = sport_key
        self.budget = budget or OddsRequestBudget(window_seconds=refresh_seconds)
        self.snapshot_path = snapshot_path
        self.max_alternate_requests = max_alternate_requests
        self._fixtures: list[Fixture] = []
        self._timeout_seconds = timeout_seconds
        self.refresh_seconds = refresh_seconds
        self.kickoff_tolerance_hours = kickoff_tolerance_hours
        self._client = client
        self._last_attempt_monotonic: float | None = None
        self._commence_time_from: datetime | None = None
        self._commence_time_to: datetime | None = None
        self._alternate_attempted: set[
            tuple[str, tuple[Decimal, Decimal]]
        ] = set()
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
            f", updated {max(quote.captured_at for quote in self.quotes):%Y-%m-%d %H:%M} UTC"
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

    def _response_json(self, response: httpx.Response) -> Any:
        self.credits_remaining = self._header_int(response, "x-requests-remaining")
        self.credits_used = self._header_int(response, "x-requests-used")
        self.last_request_cost = self._header_int(response, "x-requests-last")
        if self.credits_remaining is not None:
            self.budget.remaining = self.credits_remaining
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
            return response.json()
        except ValueError:
            raise ApiError("The Odds API returned invalid JSON") from None

    def set_fixture_window(self, fixtures: list[Fixture]) -> None:
        """Limit the provider response to the fixture block shown in the report."""
        self._fixtures = fixtures
        if not fixtures:
            self._commence_time_from = None
            self._commence_time_to = None
            return
        self._commence_time_from = min(fixture.starts_at for fixture in fixtures)
        self._commence_time_to = max(fixture.starts_at for fixture in fixtures)

    @staticmethod
    def _api_timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _request(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        try:
            if self._client is not None:
                return self._client.get(url, params=params)
            with httpx.Client(timeout=self._timeout_seconds, follow_redirects=True) as client:
                return client.get(url, params=params)
        except httpx.HTTPError as error:
            raise ApiError(
                f"The Odds API request failed ({type(error).__name__}); API key was not logged"
            ) from None

    def _get(self) -> httpx.Response:
        params: dict[str, str] = {
            "apiKey": self._api_key,
            "bookmakers": "pinnacle",
            "markets": "h2h,spreads",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if self._commence_time_from is not None:
            params["commenceTimeFrom"] = self._api_timestamp(self._commence_time_from)
        if self._commence_time_to is not None:
            params["commenceTimeTo"] = self._api_timestamp(self._commence_time_to)
        if self.budget.remaining is not None and self.budget.remaining < 2:
            # /sports is quota-free: discover allowance resets without a paid probe.
            self._response_json(self._request("https://api.the-odds-api.com/v4/sports",
                                             params={"apiKey": self._api_key}))
        self.budget.reserve(alternate=False)
        return self._request(f"https://api.the-odds-api.com/v4/sports/{self.sport_key}/odds", params=params)

    def _get_alternate_spreads(self, event_id: str) -> httpx.Response:
        self.budget.reserve(alternate=True)
        return self._request(
            f"https://api.the-odds-api.com/v4/sports/{self.sport_key}/events/{event_id}/odds",
            params={
                "apiKey": self._api_key,
                "bookmakers": "pinnacle",
                "markets": "alternate_spreads",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )

    def refresh_if_due(self, *, force: bool = False) -> bool:
        current = time.monotonic()
        if (
            not force
            and self._last_attempt_monotonic is not None
            and current - self._last_attempt_monotonic < self.refresh_seconds
        ):
            return False
        self._last_attempt_monotonic = current
        # A failed refresh must not leave old prices looking like fresh quotes.
        self.quotes = ()
        response = self._get()
        data = self._response_json(response)
        if not isinstance(data, list):
            raise ApiError("The Odds API returned an unexpected response")

        quotes: list[OddsQuote] = []
        for event in data:
            if not isinstance(event, dict):
                continue
            if event.get("sport_key", self.sport_key) != self.sport_key:
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
            h2h_market = next(
                (
                    item
                    for item in pinnacle["markets"]
                    if isinstance(item, dict)
                    and str(item.get("key", "")).casefold() == "h2h"
                ),
                None,
            )
            if h2h_market is None or not isinstance(h2h_market.get("outcomes"), list):
                continue
            prices: dict[str, Decimal] = {}
            for raw_outcome in h2h_market["outcomes"]:
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
            handicap: HandicapQuote | None = None
            spread_market = next(
                (
                    item
                    for item in pinnacle["markets"]
                    if isinstance(item, dict)
                    and str(item.get("key", "")).casefold() == "spreads"
                ),
                None,
            )
            if spread_market is not None and isinstance(spread_market.get("outcomes"), list):
                spread_values: dict[str, tuple[Decimal, Decimal]] = {}
                for raw_outcome in spread_market["outcomes"]:
                    if not isinstance(raw_outcome, dict):
                        continue
                    name = str(raw_outcome.get("name", "")).strip()
                    if normalize_team(name) == normalize_team(home_team):
                        role = "home"
                    elif normalize_team(name) == normalize_team(away_team):
                        role = "away"
                    else:
                        continue
                    try:
                        spread_values[role] = (
                            decimal_value(
                                raw_outcome.get("point"), field_name=f"{role} handicap"
                            ),
                            decimal_value(
                                raw_outcome.get("price"), field_name=f"{role} handicap odds"
                            ),
                        )
                    except ConfigError:
                        continue
                if set(spread_values) == {"home", "away"}:
                    spread_captured = (
                        spread_market.get("last_update") or pinnacle.get("last_update")
                    )
                    try:
                        handicap = HandicapQuote(
                            home_line=spread_values["home"][0],
                            away_line=spread_values["away"][0],
                            home_odds=spread_values["home"][1],
                            away_odds=spread_values["away"][1],
                            captured_at=parse_utc(
                                spread_captured, field_name="spread last_update"
                            ),
                        )
                    except ConfigError:
                        handicap = None

            captured_value = h2h_market.get("last_update") or pinnacle.get("last_update")
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
                        handicap=handicap,
                        source_event_id=str(event.get("id", "")).strip() or None,
                    )
                )
            except ConfigError:
                continue
        self.quotes = tuple(quote for quote in quotes if not self._fixtures or any(
            _matching_quote((quote,), fixture, kickoff_tolerance_hours=self.kickoff_tolerance_hours)
            for fixture in self._fixtures))
        self._alternate_attempted.clear()
        self._save_snapshot("featured")
        return True

    def _save_snapshot(self, kind: str) -> None:
        if self.snapshot_path is not None:
            save_quote_snapshot(self.snapshot_path, sport_key=self.sport_key, quotes=self.quotes, kind=kind)

    @staticmethod
    def _alternate_handicap_from_event(
        event: dict[str, Any],
        fixture: Fixture,
        selected: HandicapMarket,
    ) -> HandicapQuote | None:
        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            return None
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
            return None
        market = next(
            (
                item
                for item in pinnacle["markets"]
                if isinstance(item, dict)
                and str(item.get("key", "")).casefold() == "alternate_spreads"
            ),
            None,
        )
        if market is None or not isinstance(market.get("outcomes"), list):
            return None

        target_lines = {outcome.role: outcome.line for outcome in selected.outcomes}
        prices: dict[str, Decimal] = {}
        for raw_outcome in market["outcomes"]:
            if not isinstance(raw_outcome, dict):
                continue
            name = str(raw_outcome.get("name", "")).strip()
            if normalize_team(name) == normalize_team(fixture.home_team):
                role = "home"
            elif normalize_team(name) == normalize_team(fixture.away_team):
                role = "away"
            else:
                continue
            try:
                point = decimal_value(
                    raw_outcome.get("point"), field_name=f"{role} alternate handicap"
                )
                odds = decimal_value(
                    raw_outcome.get("price"),
                    field_name=f"{role} alternate handicap odds",
                )
            except ConfigError:
                continue
            if point == target_lines[role]:
                prices[role] = odds
        if set(prices) != {"home", "away"}:
            return None
        captured_value = market.get("last_update") or pinnacle.get("last_update")
        try:
            return HandicapQuote(
                home_line=target_lines["home"],
                away_line=target_lines["away"],
                home_odds=prices["home"],
                away_odds=prices["away"],
                captured_at=parse_utc(
                    captured_value, field_name="alternate spread last_update"
                ),
            )
        except ConfigError:
            return None

    def add_exact_alternate_handicaps(
        self,
        fixtures: list[Fixture],
        selected: dict[str, HandicapMarket],
    ) -> list[str]:
        """Fill exact selected lines from per-event alternate-spread responses."""
        warnings: list[str] = []
        for fixture in fixtures:
            market = selected.get(fixture.event_id)
            quote = self.quote_for(fixture)
            if market is None or quote is None:
                continue
            if quote.handicap is not None and all(
                quote.handicap.fair_probability(outcome.role, outcome.line) is not None
                for outcome in market.outcomes
            ):
                continue
            lines = tuple(outcome.line for outcome in market.outcomes)
            if quote.source_event_id is None:
                continue
            attempt_key = (quote.source_event_id, lines)
            if attempt_key in self._alternate_attempted:
                continue
            if len(self._alternate_attempted) >= self.max_alternate_requests:
                warnings.append("Pinnacle alternate-handicap allocation reached for this competition; remaining unmatched lines are n/a")
                break
            self._alternate_attempted.add(attempt_key)
            try:
                payload = self._response_json(
                    self._get_alternate_spreads(quote.source_event_id)
                )
            except ApiError as error:
                warnings.append(
                    f"Pinnacle alternate handicap failed for '{fixture.title}': {error}"
                )
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("id") != quote.source_event_id or payload.get("sport_key", self.sport_key) != self.sport_key:
                warnings.append(f"Ignored mismatched Pinnacle alternate event for '{fixture.title}'")
                continue
            alternate = self._alternate_handicap_from_event(payload, fixture, market)
            if alternate is None:
                continue
            self.quotes = tuple(
                replace(item, handicap=alternate)
                if item.source_event_id == quote.source_event_id
                else item
                for item in self.quotes
            )
            self._save_snapshot("alternate")
        return warnings

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
class HandicapOutcomeReport:
    outcome: HandicapOutcome
    metrics: BookMetrics | None
    error: str | None
    pinnacle_fair_probability: Decimal | None
    price_gap: Decimal | None


@dataclass(frozen=True)
class HandicapReport:
    market: HandicapMarket
    outcomes: tuple[HandicapOutcomeReport, HandicapOutcomeReport]


@dataclass(frozen=True)
class FixtureReport:
    fixture: Fixture
    outcomes: tuple[OutcomeReport, ...]
    handicap: HandicapReport | None = None


@dataclass(frozen=True)
class PremierLeagueReport:
    generated_at: datetime
    series_id: str
    stake_usd: Decimal
    odds_source: str
    fixtures: tuple[FixtureReport, ...]
    warnings: tuple[str, ...]
    available_fixture_count: int
    gameweek_start: datetime | None
    gameweek_end: datetime | None
    competition: str = "epl"


def build_premier_league_report(
    client: PolymarketPublicClient,
    *,
    odds_provider: OddsProvider | None = None,
    stake_usd: Decimal = REPORT_STAKE_CAP_USD,
    now: datetime | None = None,
    competition: str = "epl",
    event_data: tuple[str, list[dict[str, Any]]] | None = None,
    team_filter: set[str] | None = None,
) -> PremierLeagueReport:
    generated_at = (now or utc_now()).astimezone(timezone.utc)
    if stake_usd <= ZERO or stake_usd > REPORT_STAKE_CAP_USD:
        raise ConfigError(f"Report stake must be above $0 and at most ${REPORT_STAKE_CAP_USD}")
    provider = odds_provider or NoOddsProvider()
    if competition not in SPORT_KEYS:
        raise ConfigError("Unsupported report competition")
    if isinstance(provider, TheOddsApiPinnacleProvider) and provider.sport_key != SPORT_KEYS[competition]:
        raise ConfigError("Pinnacle provider competition does not match the report")
    series_id, raw_events = event_data if event_data is not None else (
        client.list_epl_events() if competition == "epl" else client.list_competition_events(competition))
    if competition == "ucl":
        if team_filter is None:
            raise ConfigError("UCL report requires an explicit current-EPL team filter")
        from polymarket_bot.competitions import regular_time_events
        raw_events = regular_time_events(raw_events)
    available_fixtures, warnings = discover_fixtures(raw_events, now=generated_at)
    if team_filter is not None:
        available_fixtures = [fixture for fixture in available_fixtures if
                              {normalize_team(fixture.home_team), normalize_team(fixture.away_team)} & team_filter]
    fixtures = select_upcoming_week(available_fixtures, now=generated_at) if competition == "epl" else available_fixtures
    if isinstance(provider, TheOddsApiPinnacleProvider) and fixtures:
        provider.set_fixture_window(fixtures)
        try:
            provider.refresh_if_due()
        except ApiError as error:
            warnings.append(f"Pinnacle refresh unavailable: {error}")
    fixture_quotes = {
        fixture.event_id: provider.quote_for(fixture) for fixture in fixtures
    }
    preferred_lines = {
        fixture.event_id: (quote.handicap.home_line, quote.handicap.away_line)
        for fixture in fixtures
        if (quote := fixture_quotes[fixture.event_id]) is not None
        and quote.handicap is not None
    }
    favorite_roles = {
        fixture.event_id: role
        for fixture in fixtures
        if (role := favorite_team_role(fixture, fixture_quotes[fixture.event_id]))
        is not None
    }
    handicaps, handicap_warnings = discover_popular_handicaps(
        raw_events,
        fixtures,
        preferred_lines=preferred_lines,
        favorite_roles=favorite_roles,
    )
    warnings.extend(handicap_warnings)
    if isinstance(provider, TheOddsApiPinnacleProvider):
        warnings.extend(provider.add_exact_alternate_handicaps(fixtures, handicaps))
        fixture_quotes = {
            fixture.event_id: provider.quote_for(fixture) for fixture in fixtures
        }

    moneyline_token_ids = [
        token_id
        for fixture in fixtures
        for outcome in fixture.outcomes
        for _, token_id in outcome.token_ids()
    ]
    handicap_token_ids = [
        outcome.market.token_id
        for handicap in handicaps.values()
        for outcome in handicap.outcomes
    ]
    token_ids = moneyline_token_ids + handicap_token_ids
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
    handicaps_without_market = 0
    handicaps_without_exact_odds = 0
    for fixture in fixtures:
        quote = fixture_quotes[fixture.event_id]
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
        handicap_report: HandicapReport | None = None
        handicap = handicaps.get(fixture.event_id)
        if handicap is None:
            handicaps_without_market += 1
        else:
            handicap_outcomes: list[HandicapOutcomeReport] = []
            has_exact_odds = False
            for outcome in handicap.outcomes:
                token_id = outcome.market.token_id
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
                    quote.handicap.fair_probability(outcome.role, outcome.line)
                    if quote and quote.handicap
                    else None
                )
                has_exact_odds = has_exact_odds or fair is not None
                execution_price = (
                    metrics.buy_vwap
                    if metrics and metrics.fillable_spend_usd >= stake_usd
                    else None
                )
                handicap_outcomes.append(
                    HandicapOutcomeReport(
                        outcome=outcome,
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
            if not has_exact_odds:
                handicaps_without_exact_odds += 1
            handicap_report = HandicapReport(
                market=handicap,
                outcomes=(handicap_outcomes[0], handicap_outcomes[1]),
            )
        fixture_reports.append(
            FixtureReport(
                fixture=fixture,
                outcomes=tuple(outcome_reports),
                handicap=handicap_report,
            )
        )
    if not fixtures:
        warnings.append(f"No eligible upcoming full-time {competition.upper()} moneyline fixtures were returned by Polymarket")
    if isinstance(provider, NoOddsProvider):
        warnings.append(
            "Pinnacle comparison is disabled; run with --pinnacle-api and a free API key"
        )
    elif fixtures_without_odds:
        warnings.append(
            f"External odds source had no matching quote for {fixtures_without_odds} of "
            f"{len(fixtures)} fixtures"
        )
    if handicaps_without_market:
        warnings.append(
            f"Polymarket had no full-match handicap for {handicaps_without_market} of "
            f"{len(fixtures)} fixtures"
        )
    if not isinstance(provider, NoOddsProvider) and handicaps_without_exact_odds:
        warnings.append(
            f"Pinnacle had no exact-line match for {handicaps_without_exact_odds} of "
            f"{len(fixtures) - handicaps_without_market} selected handicaps"
        )
    return PremierLeagueReport(
        generated_at=generated_at,
        series_id=series_id,
        stake_usd=stake_usd,
        odds_source=provider.description,
        fixtures=tuple(fixture_reports),
        warnings=tuple(warnings),
        available_fixture_count=len(available_fixtures),
        gameweek_start=fixtures[0].starts_at if fixtures else None,
        gameweek_end=fixtures[-1].starts_at if fixtures else None,
        competition=competition,
    )


def _terminal_cell(value: str, width: int) -> str:
    if len(value) > width:
        value = value[: width - 1] + "~"
    return value.ljust(width)


def render_terminal(report: PremierLeagueReport) -> str:
    token_book_count = sum(
        len(fixture.outcomes)
        + (len(fixture.handicap.outcomes) if fixture.handicap is not None else 0)
        for fixture in report.fixtures
    )
    healthy_count = sum(
        outcome.error is None
        for fixture in report.fixtures
        for outcome in fixture.outcomes
    ) + sum(
        outcome.error is None
        for fixture in report.fixtures
        if fixture.handicap is not None
        for outcome in fixture.handicap.outcomes
    )
    lines = [
        "Premier League market monitor" if report.competition == "epl" else "Champions League market monitor | Premier League teams only",
        (
            f"Updated {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')} UTC | "
            f"{len(report.fixtures)} fixtures | {healthy_count}/{token_book_count} books healthy | "
            f"Pinnacle: {report.odds_source}"
        ),
    ]
    if report.gameweek_start is not None and report.gameweek_end is not None:
        lines.append(
            ("Scope: EPL fixtures in the next 7 days " if report.competition == "epl" else "Scope: all listed upcoming UCL fixtures involving EPL teams ") +
            f"{report.gameweek_start:%Y-%m-%d %H:%M} to "
            f"{report.gameweek_end:%Y-%m-%d %H:%M} UTC "
            f"({len(report.fixtures)} of {report.available_fixture_count} upcoming fixtures)"
        )
    lines.extend(f"WARNING: {warning}" for warning in report.warnings)
    header = (
        f"{_terminal_cell('Outcome', OUTCOME_COLUMN_WIDTH)} "
        f"{'Tok':<3} {'Bid':>6} {'Ask':>6} {'Spr':>6} "
        f"{'Bid$2c':>10} {'Ask$2c':>10} {'BuyVWAP':>7} "
        f"{'PinFair':>8} {'Gap':>8}"
    )
    divider = "-" * len(header)

    def price(value: Decimal | None) -> str:
        return f"{value:.3f}" if value is not None else "n/a"

    def usd(value: Decimal | None) -> str:
        return f"${value:,.0f}" if value is not None else "n/a"

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
            lines.append(
                f"{_terminal_cell(outcome_report.outcome.label, OUTCOME_COLUMN_WIDTH)} "
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
        if fixture_report.handicap is not None:
            handicap = fixture_report.handicap
            lines.extend(
                [
                    (
                        "  Favourite-team handicap "
                        f"(volume ${handicap.market.volume:,.0f}; "
                        f"liquidity ${handicap.market.liquidity:,.0f})"
                    ),
                    f"  {handicap.market.url}",
                ]
            )
            for outcome_report in handicap.outcomes:
                metrics = outcome_report.metrics
                fair = outcome_report.pinnacle_fair_probability
                gap = outcome_report.price_gap
                lines.append(
                    f"{_terminal_cell(outcome_report.outcome.label, OUTCOME_COLUMN_WIDTH)} "
                    f"{'HCP':<3} "
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
            "HCP is the favourite's highest-volume negative full-match handicap; liquidity breaks ties.",
            "Handicap PinFair/Gap use Pinnacle's featured or alternate price only at the exact line.",
            "Only half-goal handicaps are compared; whole/quarter lines have different payout rules.",
            "Prices are before fees. UCL markets require explicit 90-minute settlement terms; qualification is excluded.",
        ]
    )
    return "\n".join(lines)
