from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import httpx

from polymarket_bot.errors import ApiError, MarketResolutionError
from polymarket_bot.models import MarketInfo, OrderBook


GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


def _json_list(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise MarketResolutionError(f"Invalid {name} JSON returned by Polymarket") from error
        if isinstance(parsed, list):
            return parsed
    raise MarketResolutionError(f"Polymarket returned an invalid {name} field")


class PolymarketPublicClient:
    def __init__(self, *, timeout_seconds: float = 10.0, client: httpx.Client | None = None):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "polymarket-premier-league-monitor/0.2"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "PolymarketPublicClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, url: str, **kwargs: Any) -> Any:
        try:
            response = self.client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ApiError(f"GET {url} failed: {error}") from error

    def _post(self, url: str, **kwargs: Any) -> Any:
        try:
            response = self.client.post(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ApiError(f"POST {url} failed: {error}") from error

    def get_order_book(self, token_id: str) -> OrderBook:
        data = self._get(f"{CLOB_URL}/book", params={"token_id": token_id})
        if not isinstance(data, dict):
            raise ApiError("Order-book endpoint returned an unexpected response")
        book = OrderBook.from_api(data)
        if book.token_id != token_id:
            raise ApiError("Order-book token ID did not match the request")
        return book

    def get_order_books(self, token_ids: Iterable[str]) -> dict[str, OrderBook]:
        """Fetch public books in bounded batches and key them by requested token ID."""
        requested = list(dict.fromkeys(str(token_id) for token_id in token_ids))
        books: dict[str, OrderBook] = {}
        for start in range(0, len(requested), 50):
            batch = requested[start : start + 50]
            data = self._post(
                f"{CLOB_URL}/books",
                json=[{"token_id": token_id} for token_id in batch],
            )
            if not isinstance(data, list):
                raise ApiError("Batch order-book endpoint returned an unexpected response")
            for raw in data:
                if not isinstance(raw, dict):
                    raise ApiError("Batch order-book endpoint returned an invalid book")
                book = OrderBook.from_api(raw)
                if book.token_id not in batch:
                    raise ApiError("Batch order-book token ID was not requested")
                books[book.token_id] = book
        return books

    def epl_series_id(self) -> str:
        sports = self._get(f"{GAMMA_URL}/sports")
        if not isinstance(sports, list):
            raise ApiError("Sports endpoint returned an unexpected response")
        matches = [
            sport
            for sport in sports
            if isinstance(sport, dict) and str(sport.get("sport", "")).casefold() == "epl"
        ]
        if len(matches) != 1 or not str(matches[0].get("series", "")).strip():
            raise MarketResolutionError("Could not identify the active EPL sports series")
        return str(matches[0]["series"])

    def list_epl_events(self, *, max_pages: int = 20) -> tuple[str, list[dict[str, Any]]]:
        """Return every active event currently attached to Polymarket's EPL series."""
        series_id = self.epl_series_id()
        events: list[dict[str, Any]] = []
        page_size = 100  # Gamma currently caps list responses at 100.
        for page in range(max_pages):
            data = self._get(
                f"{GAMMA_URL}/events",
                params={
                    "series_id": series_id,
                    "active": "true",
                    "closed": "false",
                    "limit": page_size,
                    "offset": page * page_size,
                },
            )
            if not isinstance(data, list):
                raise ApiError("Events endpoint returned an unexpected response")
            valid = [event for event in data if isinstance(event, dict)]
            events.extend(valid)
            if len(data) < page_size:
                return series_id, events
        raise ApiError("EPL event pagination exceeded the safety limit")

def _market_info(raw: dict[str, Any], token_id: str) -> MarketInfo:
    return MarketInfo(
        condition_id=str(raw.get("conditionId", "")),
        token_id=token_id,
    )
