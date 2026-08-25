from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_bot.errors import ConfigError


def decimal_value(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a decimal, not a boolean")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be a decimal") from error
    if not result.is_finite():
        raise ConfigError(f"{field_name} must be finite")
    return result


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MarketInfo:
    condition_id: str
    token_id: str


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    condition_id: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    tick_size: Decimal
    min_order_size: Decimal
    neg_risk: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "OrderBook":
        def levels(name: str) -> tuple[PriceLevel, ...]:
            raw_levels = data.get(name, [])
            if not isinstance(raw_levels, list):
                raise ConfigError(f"Order book {name} must be a list")
            return tuple(
                PriceLevel(
                    price=decimal_value(item["price"], field_name=f"{name}.price"),
                    size=decimal_value(item["size"], field_name=f"{name}.size"),
                )
                for item in raw_levels
            )

        return cls(
            token_id=str(data.get("asset_id", "")),
            condition_id=str(data.get("market", "")),
            bids=levels("bids"),
            asks=levels("asks"),
            tick_size=decimal_value(data.get("tick_size"), field_name="tick_size"),
            min_order_size=decimal_value(
                data.get("min_order_size"), field_name="min_order_size"
            ),
            neg_risk=bool(data.get("neg_risk", False)),
        )

    @property
    def best_bid(self) -> Decimal | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> Decimal | None:
        return min((level.price for level in self.asks), default=None)
