"""Shared request budget and credential-free bookmaker quote snapshots."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from polymarket_bot.errors import ApiError


class OddsRequestBudget:
    """Bound a running monitor's combined usage; reserve bulk refresh headroom.

    Budget is process-local. Provider quota headers additionally guard the actual
    account allowance, including usage by other processes. Failed requests count
    conservatively until the next budget window.
    """
    def __init__(self, *, competitions: int = 1, window_seconds: float = 72000):
        self.window_seconds = window_seconds
        self.bulk_limit = 2 * competitions
        self.alternate_limit = 12 - self.bulk_limit
        self.started = time.monotonic()
        self.used = {"bulk": 0, "alternate": 0}
        self.remaining: int | None = None

    def reserve(self, *, alternate: bool) -> None:
        now = time.monotonic()
        if now - self.started >= self.window_seconds:
            self.started = now
            self.used = {"bulk": 0, "alternate": 0}
        kind, cost = ("alternate", 1) if alternate else ("bulk", 2)
        limit = self.alternate_limit if alternate else self.bulk_limit
        if self.used[kind] + cost > limit:
            raise ApiError(f"Shared Pinnacle {kind} request budget exhausted; waiting for the next refresh window")
        if self.remaining is not None and self.remaining < cost:
            raise ApiError("Insufficient remaining The Odds API credits; request skipped")
        self.used[kind] += cost
        if self.remaining is not None:
            self.remaining -= cost


def save_quote_snapshot(path: str | Path, *, sport_key: str, quotes: tuple[Any, ...], kind: str) -> None:
    """Append only parsed odds, timestamps and IDs; never request URLs/API keys."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, sport_key TEXT NOT NULL,
            kind TEXT NOT NULL, quotes_json TEXT NOT NULL)""")
        db.execute("INSERT INTO odds_snapshots(fetched_at,sport_key,kind,quotes_json) VALUES(?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), sport_key, kind,
                    json.dumps([asdict(quote) for quote in quotes], default=str)))
