from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_bot.errors import ConfigError
from polymarket_bot.models import decimal_value


def _integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be an integer") from error


@dataclass(frozen=True)
class BetTrade:
    id: str
    user: str
    timestamp: int
    condition_id: str
    asset: str
    side: str
    size: Decimal
    usdc_size: Decimal
    price: Decimal
    title: str
    event_slug: str
    outcome: str
    outcome_index: int
    transaction_hash: str
    raw_json: str

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, user: str) -> "BetTrade":
        timestamp = _integer(raw.get("timestamp"), field_name="trade timestamp")
        side = str(raw.get("side", "")).upper()
        if side not in {"BUY", "SELL"}:
            raise ConfigError(f"Unsupported trade side: {side or 'missing'}")
        price = decimal_value(raw.get("price"), field_name="trade price")
        size = decimal_value(raw.get("size"), field_name="trade size")
        usdc_raw = raw.get("usdcSize")
        usdc_size = (
            decimal_value(usdc_raw, field_name="trade USDC size")
            if usdc_raw not in {None, ""}
            else price * size
        )
        identity = "|".join(
            [
                user.lower(),
                str(timestamp),
                str(raw.get("transactionHash", "")),
                str(raw.get("conditionId", "")),
                str(raw.get("asset", "")),
                side,
                str(price),
                str(size),
            ]
        )
        return cls(
            id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            user=user.lower(),
            timestamp=timestamp,
            condition_id=str(raw.get("conditionId", "")),
            asset=str(raw.get("asset", "")),
            side=side,
            size=size,
            usdc_size=usdc_size,
            price=price,
            title=str(raw.get("title", "")).strip(),
            event_slug=str(raw.get("eventSlug", "")).strip(),
            outcome=str(raw.get("outcome", "")).strip(),
            outcome_index=_integer(raw.get("outcomeIndex", 0), field_name="outcome index"),
            transaction_hash=str(raw.get("transactionHash", "")).strip(),
            raw_json=json.dumps(raw, sort_keys=True, separators=(",", ":")),
        )

    @property
    def placed_at(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


@dataclass(frozen=True)
class ClosedPosition:
    id: str
    user: str
    asset: str
    condition_id: str
    event_slug: str
    outcome: str
    average_price: Decimal
    total_bought: Decimal
    realized_pnl: Decimal
    timestamp: int
    raw_json: str

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, user: str) -> "ClosedPosition":
        asset = str(raw.get("asset", ""))
        event_slug = str(raw.get("eventSlug", "")).strip()
        identity = f"{user.lower()}|{asset}|{event_slug}"
        return cls(
            id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            user=user.lower(),
            asset=asset,
            condition_id=str(raw.get("conditionId", "")),
            event_slug=event_slug,
            outcome=str(raw.get("outcome", "")).strip(),
            average_price=decimal_value(raw.get("avgPrice", 0), field_name="average price"),
            total_bought=decimal_value(raw.get("totalBought", 0), field_name="total bought"),
            realized_pnl=decimal_value(raw.get("realizedPnl", 0), field_name="realized P/L"),
            timestamp=_integer(raw.get("timestamp", 0), field_name="position timestamp"),
            raw_json=json.dumps(raw, sort_keys=True, separators=(",", ":")),
        )


@dataclass(frozen=True)
class MarketEvent:
    event_slug: str
    title: str
    kickoff: datetime
    home_team: str
    away_team: str
    condition_roles: dict[str, str]
    winning_outcomes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchXg:
    match_id: str
    season: int
    kickoff: datetime
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    home_xg: Decimal
    away_xg: Decimal
    source: str = "Understat"


@dataclass(frozen=True)
class AnalyzedBet:
    trade: BetTrade
    event: MarketEvent | None
    match: MatchXg | None
    role: str | None
    token_side: str | None
    market_type: str
    direction_team: str | None
    direction_opponent: str | None
    direction_is_home: bool | None
    result: str
    resolution_source: str | None
    hold_pnl: Decimal | None
