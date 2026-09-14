from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from polymarket_bot.analytics_models import BetTrade, ClosedPosition, MarketEvent, MatchXg
from polymarket_bot.errors import ApiError, ConfigError
from polymarket_bot.reporting import normalize_team, parse_utc


DATA_API_URL = "https://data-api.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
PROFILE_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")


def validate_profile_address(value: str) -> str:
    address = value.strip().lower()
    if not PROFILE_ADDRESS_PATTERN.fullmatch(address):
        raise ConfigError("Polymarket profile address must be 0x followed by 40 hex characters")
    return address


def _api_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _resolved_winner(market: dict[str, Any]) -> str | None:
    if market.get("closed") is not True:
        return None
    if str(market.get("umaResolutionStatus", "")).casefold() != "resolved":
        return None
    outcomes = _api_array(market.get("outcomes"))
    prices = _api_array(market.get("outcomePrices"))
    if len(outcomes) != len(prices):
        return None
    try:
        winning_indexes = [
            index for index, price in enumerate(prices) if Decimal(str(price)) == 1
        ]
    except (InvalidOperation, TypeError, ValueError):
        return None
    if len(winning_indexes) != 1:
        return None
    winner = str(outcomes[winning_indexes[0]]).strip()
    return winner or None


def season_bounds(season: int) -> tuple[datetime, datetime]:
    if not 2014 <= season <= 2100:
        raise ConfigError("Season must be its four-digit starting year")
    return (
        datetime(season, 7, 1, tzinfo=timezone.utc),
        datetime(season + 1, 7, 1, tzinfo=timezone.utc),
    )


def _event_date_from_slug(slug: str) -> datetime | None:
    match = re.search(r"-(\d{4})-(\d{2})-(\d{2})(?:-|$)", slug)
    if match is None:
        return None
    try:
        return datetime(*(int(value) for value in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


def is_season_epl_slug(
    slug: str, season: int, *, timestamp: int | None = None
) -> bool:
    if not slug.casefold().startswith("epl-"):
        return False
    start, end = season_bounds(season)
    event_date = _event_date_from_slug(slug)
    if event_date is not None:
        return start <= event_date < end
    if timestamp is None:
        return True
    placed_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return start <= placed_at < end


def is_season_epl_fixture_slug(slug: str, season: int) -> bool:
    if not slug.casefold().startswith("epl-"):
        return False
    event_date = _event_date_from_slug(slug)
    if event_date is None:
        return False
    start, end = season_bounds(season)
    return start <= event_date < end


SUPPORTED_COMPETITIONS = ("epl", "ucl")
DEFAULT_HISTORY_COMPETITIONS = ("epl",)
COMPETITION_SLUG_PREFIXES = {
    "epl": ("epl-",),
    "ucl": ("ucl-", "champions-league-", "uefa-champions-league-"),
}


def is_season_competition_slug(
    slug: str,
    season: int,
    *,
    competitions: tuple[str, ...] = SUPPORTED_COMPETITIONS,
    timestamp: int | None = None,
) -> bool:
    """Return whether a Polymarket football slug belongs to this season/scope."""
    normalized = tuple(item.casefold() for item in competitions)
    if any(item not in SUPPORTED_COMPETITIONS for item in normalized):
        raise ConfigError("Unsupported football competition")
    lowered_slug = slug.casefold()
    if not any(
        lowered_slug.startswith(prefix)
        for item in normalized
        for prefix in COMPETITION_SLUG_PREFIXES[item]
    ):
        return False
    start, end = season_bounds(season)
    event_date = _event_date_from_slug(slug)
    if event_date is not None:
        return start <= event_date < end
    if timestamp is None:
        return True
    placed_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return start <= placed_at < end


class PolymarketHistoryClient:
    def __init__(self, *, timeout_seconds: float = 20.0, client: httpx.Client | None = None):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "premier-league-bet-analytics/0.1"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "PolymarketHistoryClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ApiError(f"GET {url} failed: {error}") from error

    def _pages(
        self,
        path: str,
        *,
        params: dict[str, Any],
        limit: int = 500,
        max_offset: int = 10000,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for offset in range(0, max_offset + 1, limit):
            page_params = {**params, "limit": limit, "offset": offset}
            data = self._get(f"{DATA_API_URL}/{path}", params=page_params)
            if not isinstance(data, list):
                raise ApiError(f"Polymarket {path} endpoint returned an unexpected response")
            page = [row for row in data if isinstance(row, dict)]
            rows.extend(page)
            if len(data) < limit:
                return rows
        raise ApiError(f"Polymarket {path} pagination exceeded {max_offset:,} records")

    def trades_for_season(
        self,
        user: str,
        season: int,
        *,
        competitions: tuple[str, ...] = DEFAULT_HISTORY_COMPETITIONS,
    ) -> list[BetTrade]:
        address = validate_profile_address(user)
        start, end = season_bounds(season)
        rows = self._pages(
            "activity",
            params={
                "user": address,
                "type": "TRADE",
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
            },
        )
        trades: list[BetTrade] = []
        for row in rows:
            slug = str(row.get("eventSlug", "")).strip()
            if is_season_competition_slug(slug, season, competitions=competitions):
                trades.append(BetTrade.from_api(row, user=address))
        return trades

    def closed_positions_for_season(
        self,
        user: str,
        season: int,
        *,
        competitions: tuple[str, ...] = DEFAULT_HISTORY_COMPETITIONS,
    ) -> list[ClosedPosition]:
        address = validate_profile_address(user)
        rows = self._pages(
            "closed-positions",
            params={"user": address, "sortBy": "TIMESTAMP", "sortDirection": "ASC"},
            limit=50,
            max_offset=100000,
        )
        positions = [ClosedPosition.from_api(row, user=address) for row in rows]
        return [
            position
            for position in positions
            if is_season_competition_slug(
                position.event_slug,
                season,
                competitions=competitions,
                timestamp=position.timestamp,
            )
        ]

    def event(self, event_slug: str) -> MarketEvent:
        data = self._get(f"{GAMMA_API_URL}/events/slug/{event_slug}")
        if not isinstance(data, dict):
            raise ApiError(f"Gamma returned an invalid event for {event_slug}")
        title = str(data.get("title", "")).strip()
        fixture_title = re.sub(r"\s+-\s+More Markets$", "", title, flags=re.IGNORECASE)
        teams = re.split(r"\s+vs\.?\s+", fixture_title, maxsplit=1, flags=re.IGNORECASE)
        if len(teams) != 2 or not all(team.strip() for team in teams):
            raise ConfigError(f"Could not identify teams for Polymarket event {event_slug}")
        home_team, away_team = (team.strip() for team in teams)
        kickoff = parse_utc(
            data.get("eventStartTime") or data.get("endDate"), field_name="event kickoff"
        )
        condition_roles: dict[str, str] = {}
        winning_outcomes: dict[str, str] = {}
        markets = data.get("markets")
        if not isinstance(markets, list):
            raise ConfigError(f"Polymarket event {event_slug} has no markets")
        home_key = normalize_team(home_team)
        away_key = normalize_team(away_team)
        for market in markets:
            if not isinstance(market, dict):
                continue
            condition_id = str(market.get("conditionId", "")).strip()
            winner = _resolved_winner(market)
            if condition_id and winner:
                winning_outcomes[condition_id] = winner
            if market.get("sportsMarketType") != "moneyline":
                continue
            group_title = str(market.get("groupItemTitle", "")).strip()
            question = str(market.get("question", "")).casefold()
            group_key = normalize_team(group_title)
            if "draw" in group_title.casefold() or "end in a draw" in question:
                role = "draw"
            elif group_key == home_key:
                role = "home"
            elif group_key == away_key:
                role = "away"
            else:
                continue
            if condition_id:
                condition_roles[condition_id] = role
        return MarketEvent(
            event_slug=event_slug,
            title=fixture_title,
            kickoff=kickoff,
            home_team=home_team,
            away_team=away_team,
            condition_roles=condition_roles,
            winning_outcomes=winning_outcomes,
        )


class AnalyticsStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "AnalyticsStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS profiles (
                user TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                user TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                condition_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                size TEXT NOT NULL,
                usdc_size TEXT NOT NULL,
                price TEXT NOT NULL,
                title TEXT NOT NULL,
                event_slug TEXT NOT NULL,
                outcome TEXT NOT NULL,
                outcome_index INTEGER NOT NULL,
                transaction_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS trades_user_time ON trades(user, timestamp);
            CREATE TABLE IF NOT EXISTS closed_positions (
                id TEXT PRIMARY KEY,
                user TEXT NOT NULL,
                asset TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                event_slug TEXT NOT NULL,
                outcome TEXT NOT NULL,
                average_price TEXT NOT NULL,
                total_bought TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS closed_user ON closed_positions(user);
            CREATE TABLE IF NOT EXISTS market_events (
                event_slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                kickoff_utc TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                condition_roles_json TEXT NOT NULL,
                winning_outcomes_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS xg_matches (
                match_id TEXT PRIMARY KEY,
                season INTEGER NOT NULL,
                kickoff_utc TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER NOT NULL,
                away_goals INTEGER NOT NULL,
                home_xg TEXT NOT NULL,
                away_xg TEXT NOT NULL,
                source TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS xg_season_time ON xg_matches(season, kickoff_utc);
            """
        )
        event_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(market_events)")
        }
        if "winning_outcomes_json" not in event_columns:
            self.connection.execute(
                """
                ALTER TABLE market_events
                ADD COLUMN winning_outcomes_json TEXT NOT NULL DEFAULT '{}'
                """
            )
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_trades(self, trades: Iterable[BetTrade]) -> tuple[int, int]:
        inserted = 0
        seen = 0
        with self.connection:
            for trade in trades:
                seen += 1
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO trades VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade.id,
                        trade.user,
                        trade.timestamp,
                        trade.condition_id,
                        trade.asset,
                        trade.side,
                        str(trade.size),
                        str(trade.usdc_size),
                        str(trade.price),
                        trade.title,
                        trade.event_slug,
                        trade.outcome,
                        trade.outcome_index,
                        trade.transaction_hash,
                        trade.raw_json,
                        self._now(),
                    ),
                )
                inserted += cursor.rowcount
        return inserted, seen

    def upsert_closed_positions(self, positions: Iterable[ClosedPosition]) -> int:
        count = 0
        with self.connection:
            for position in positions:
                self.connection.execute(
                    """
                    INSERT INTO closed_positions VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        average_price=excluded.average_price,
                        total_bought=excluded.total_bought,
                        realized_pnl=excluded.realized_pnl,
                        timestamp=excluded.timestamp,
                        raw_json=excluded.raw_json,
                        synced_at=excluded.synced_at
                    """,
                    (
                        position.id,
                        position.user,
                        position.asset,
                        position.condition_id,
                        position.event_slug,
                        position.outcome,
                        str(position.average_price),
                        str(position.total_bought),
                        str(position.realized_pnl),
                        position.timestamp,
                        position.raw_json,
                        self._now(),
                    ),
                )
                count += 1
        return count

    def upsert_event(self, event: MarketEvent) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO market_events (
                    event_slug, title, kickoff_utc, home_team, away_team,
                    condition_roles_json, winning_outcomes_json, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_slug) DO UPDATE SET
                    title=excluded.title,
                    kickoff_utc=excluded.kickoff_utc,
                    home_team=excluded.home_team,
                    away_team=excluded.away_team,
                    condition_roles_json=excluded.condition_roles_json,
                    winning_outcomes_json=excluded.winning_outcomes_json,
                    synced_at=excluded.synced_at
                """,
                (
                    event.event_slug,
                    event.title,
                    event.kickoff.isoformat(),
                    event.home_team,
                    event.away_team,
                    json.dumps(event.condition_roles, sort_keys=True),
                    json.dumps(event.winning_outcomes, sort_keys=True),
                    self._now(),
                ),
            )

    def upsert_xg_matches(self, matches: Iterable[MatchXg]) -> int:
        count = 0
        with self.connection:
            for match in matches:
                self.connection.execute(
                    """
                    INSERT INTO xg_matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(match_id) DO UPDATE SET
                        kickoff_utc=excluded.kickoff_utc,
                        home_team=excluded.home_team,
                        away_team=excluded.away_team,
                        home_goals=excluded.home_goals,
                        away_goals=excluded.away_goals,
                        home_xg=excluded.home_xg,
                        away_xg=excluded.away_xg,
                        source=excluded.source,
                        synced_at=excluded.synced_at
                    """,
                    (
                        match.match_id,
                        match.season,
                        match.kickoff.isoformat(),
                        match.home_team,
                        match.away_team,
                        match.home_goals,
                        match.away_goals,
                        str(match.home_xg),
                        str(match.away_xg),
                        match.source,
                        self._now(),
                    ),
                )
                count += 1
        return count

    def known_users(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT user FROM profiles
            UNION
            SELECT DISTINCT user FROM trades
            ORDER BY user
            """
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def remember_user(self, user: str) -> None:
        address = validate_profile_address(user)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO profiles(user, added_at) VALUES (?, ?)",
                (address, self._now()),
            )

    def trades(
        self,
        user: str,
        season: int,
        *,
        competitions: tuple[str, ...] = DEFAULT_HISTORY_COMPETITIONS,
    ) -> list[BetTrade]:
        start, end = season_bounds(season)
        rows = self.connection.execute(
            """
            SELECT * FROM trades
            WHERE user=? AND timestamp>=? AND timestamp<?
            ORDER BY timestamp
            """,
            (user.lower(), int(start.timestamp()), int(end.timestamp())),
        ).fetchall()
        trades = [
            BetTrade(
                id=row["id"],
                user=row["user"],
                timestamp=row["timestamp"],
                condition_id=row["condition_id"],
                asset=row["asset"],
                side=row["side"],
                size=Decimal(row["size"]),
                usdc_size=Decimal(row["usdc_size"]),
                price=Decimal(row["price"]),
                title=row["title"],
                event_slug=row["event_slug"],
                outcome=row["outcome"],
                outcome_index=row["outcome_index"],
                transaction_hash=row["transaction_hash"],
                raw_json=row["raw_json"],
            )
            for row in rows
        ]
        return [
            trade
            for trade in trades
            if is_season_competition_slug(
                trade.event_slug,
                season,
                competitions=competitions,
                timestamp=trade.timestamp,
            )
        ]

    def events(self) -> dict[str, MarketEvent]:
        rows = self.connection.execute("SELECT * FROM market_events").fetchall()
        return {
            row["event_slug"]: MarketEvent(
                event_slug=row["event_slug"],
                title=row["title"],
                kickoff=datetime.fromisoformat(row["kickoff_utc"]),
                home_team=row["home_team"],
                away_team=row["away_team"],
                condition_roles=json.loads(row["condition_roles_json"]),
                winning_outcomes=json.loads(row["winning_outcomes_json"]),
            )
            for row in rows
        }

    def xg_matches(self, season: int) -> list[MatchXg]:
        rows = self.connection.execute(
            "SELECT * FROM xg_matches WHERE season=? ORDER BY kickoff_utc", (season,)
        ).fetchall()
        return [
            MatchXg(
                match_id=row["match_id"],
                season=row["season"],
                kickoff=datetime.fromisoformat(row["kickoff_utc"]),
                home_team=row["home_team"],
                away_team=row["away_team"],
                home_goals=row["home_goals"],
                away_goals=row["away_goals"],
                home_xg=Decimal(row["home_xg"]),
                away_xg=Decimal(row["away_xg"]),
                source=row["source"],
            )
            for row in rows
        ]

    def realized_pnl(
        self,
        user: str,
        season: int,
        *,
        competitions: tuple[str, ...] = DEFAULT_HISTORY_COMPETITIONS,
    ) -> tuple[int, Decimal]:
        rows = self.connection.execute(
            "SELECT event_slug, timestamp, realized_pnl FROM closed_positions WHERE user=?",
            (user.lower(),),
        ).fetchall()
        selected = [
            row
            for row in rows
            if is_season_competition_slug(
                row["event_slug"],
                season,
                competitions=competitions,
                timestamp=row["timestamp"],
            )
        ]
        return len(selected), sum((Decimal(row["realized_pnl"]) for row in selected), Decimal("0"))
