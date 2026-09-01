from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from polymarket_bot.analytics_models import MatchXg
from polymarket_bot.errors import ApiError, ConfigError
from polymarket_bot.models import decimal_value


UNDERSTAT_BASE_URL = "https://understat.com"


class UnderstatXgClient:
    """Unofficial adapter for the JSON requested by Understat's public league page."""

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

    def __enter__(self) -> "UnderstatXgClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _kickoff(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ConfigError("Understat kickoff must be a timestamp")
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError as error:
            raise ConfigError("Understat kickoff had an unexpected format") from error

    def matches(self, season: int) -> list[MatchXg]:
        url = f"{UNDERSTAT_BASE_URL}/getLeagueData/EPL/{season}"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{UNDERSTAT_BASE_URL}/league/EPL/{season}",
        }
        try:
            response = self.client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ApiError(f"Understat EPL xG request failed: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("dates"), list):
            raise ApiError("Understat returned an unexpected EPL xG response")

        matches: list[MatchXg] = []
        for raw in data["dates"]:
            if not isinstance(raw, dict) or raw.get("isResult") is not True:
                continue
            home = raw.get("h")
            away = raw.get("a")
            goals = raw.get("goals")
            xg = raw.get("xG")
            if not all(isinstance(value, dict) for value in (home, away, goals, xg)):
                continue
            try:
                matches.append(
                    MatchXg(
                        match_id=str(raw["id"]),
                        season=season,
                        kickoff=self._kickoff(raw.get("datetime")),
                        home_team=str(home["title"]),
                        away_team=str(away["title"]),
                        home_goals=int(goals["h"]),
                        away_goals=int(goals["a"]),
                        home_xg=decimal_value(xg["h"], field_name="home xG"),
                        away_xg=decimal_value(xg["a"], field_name="away xG"),
                    )
                )
            except (ConfigError, KeyError, TypeError, ValueError):
                continue
        return matches
