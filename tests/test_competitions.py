from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from polymarket_bot.competitions import build_market_reports, current_epl_teams, regular_time_events
from polymarket_bot.errors import ApiError, ConfigError
from polymarket_bot.market_data import PolymarketPublicClient
from polymarket_bot.models import OrderBook
from polymarket_bot.odds_audit import OddsRequestBudget
from polymarket_bot.report_cli import _parser
from polymarket_bot.reporting import (
    HandicapQuote, TheOddsApiPinnacleProvider, build_premier_league_report,
    discover_popular_handicaps, moneyline_fixture_from_event, normalize_team, render_terminal,
)

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
KICKOFF = "2026-09-08T19:00:00Z"


def event(key="ucl-0", home="Club 0", away="FC Porto", *, kickoff=KICKOFF):
    markets = []
    for role, team in [("home", home), ("draw", "Draw"), ("away", away)]:
        markets.append(dict(conditionId=f"{key}-{role}", sportsMarketType="moneyline",
                            groupItemTitle=team, question=f"Will {team} win?",
                            description="This market refers only to the first 90 minutes of regular play plus stoppage time.",
                            outcomes=["Yes", "No"], clobTokenIds=[f"{key}-{role}-yes", f"{key}-{role}-no"]))
    markets.append(dict(conditionId=f"{key}-spread", sportsMarketType="spreads", line="-1.5",
                        question=f"Spread: {home} (-1.5)", description=markets[0]["description"],
                        outcomes=[home, away], clobTokenIds=[f"{key}-spread-home", f"{key}-spread-away"]))
    return dict(id=key, slug=key, title=f"{home} vs. {away}", eventStartTime=kickoff, markets=markets)


def epl_events():
    return [event(f"epl-{i}", f"Club {i * 2}", f"Club {i * 2 + 1}") for i in range(10)]


class FakeClient:
    def __init__(self):
        self.epl = epl_events()
        self.ucl = [event(), event("ucl-foreign", "Real Madrid", "Barcelona")]
        self.requested = []
        self.fail_ucl = False

    def list_epl_events(self):
        return "epl-series", self.epl

    def list_competition_events(self, competition):
        assert competition == "ucl"
        if self.fail_ucl:
            raise ApiError("UCL unavailable")
        return "ucl-series", self.ucl

    def get_order_books(self, token_ids):
        self.requested.extend(token_ids)
        conditions = {token: m["conditionId"] for e in self.epl + self.ucl for m in e["markets"] for token in m["clobTokenIds"]}
        return {token: OrderBook.from_api(dict(asset_id=token, market=conditions[token], timestamp="1000",
                  tick_size="0.01", min_order_size="5", bids=[dict(price="0.39", size="100")],
                  asks=[dict(price="0.4", size="100")])) for token in token_ids}


def odds_event(sport="soccer_uefa_champs_league", *, spread=True):
    markets = [dict(key="h2h", last_update=NOW.isoformat(), outcomes=[
        dict(name="Club 0", price=2), dict(name="Draw", price=4), dict(name="Porto", price=4)])]
    if spread:
        markets.append(dict(key="spreads", last_update=NOW.isoformat(), outcomes=[
            dict(name="Club 0", point=-1.5, price=2), dict(name="Porto", point=1.5, price=2)]))
    return dict(id="odds-ucl", sport_key=sport, home_team="Club 0", away_team="Porto", commence_time=KICKOFF,
                bookmakers=[dict(key="pinnacle", markets=markets)])


class CompetitionTests(unittest.TestCase):
    def test_cli_defaults_to_both_and_allows_ucl_only(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_parser().parse_args([]).competitions, "both")
        self.assertEqual(_parser().parse_args(["--competitions", "ucl"]).competitions, "ucl")

    def test_current_season_roster_excludes_old_events_and_requires_20(self):
        events = epl_events()
        self.assertEqual(len(current_epl_teams(events + [event("epl-old", "Relegated", "Old club", kickoff="2025-09-08T19:00:00Z")], now=NOW)), 20)
        with self.assertRaisesRegex(ConfigError, "not 20"):
            current_epl_teams(events[:1], now=NOW)

    def test_ucl_scope_includes_home_away_and_two_epl_teams_once(self):
        client = FakeClient()
        client.ucl.extend([event("ucl-away", "Napoli", "Club 1"), event("ucl-both", "Club 0", "Club 1"),
                           event("ucl-later", "Club 2", "PSG", kickoff="2026-10-10T19:00:00Z")])
        reports, warnings = build_market_reports(client, now=NOW)
        self.assertFalse(warnings)
        self.assertEqual([len(r.fixtures) for r in reports], [10, 4])
        self.assertFalse(any("foreign" in token for token in client.requested))
        self.assertIn("Champions League", render_terminal(reports[1]))
        self.assertIn("all listed upcoming", render_terminal(reports[1]))

    def test_ucl_failure_preserves_epl_section(self):
        client = FakeClient()
        client.fail_ucl = True
        reports, warnings = build_market_reports(client, now=NOW)
        self.assertEqual([r.competition for r in reports], ["epl"])
        self.assertTrue(any("UCL section unavailable" in w for w in warnings))

    def test_incomplete_roster_fails_closed_for_ucl_only(self):
        client = FakeClient()
        client.epl = client.epl[:1]
        reports, warnings = build_market_reports(client, competitions=("ucl",), now=NOW)
        self.assertFalse(reports)
        self.assertIn("not 20", warnings[0])
        self.assertFalse(client.requested)

    def test_only_explicit_regular_time_markets_are_kept(self):
        self.assertEqual(len(regular_time_events([event()])), 1)
        for text in ["First half winner", "To qualify", "Aggregate winner", "Extra time winner"]:
            sample = event()
            sample["title"] = text
            self.assertFalse(regular_time_events([sample]))
        for description in ["Winner", "90 minutes including extra time", "90 minutes including penalties"]:
            sample = event()
            for market in sample["markets"]:
                market["description"] = description
            self.assertFalse(regular_time_events([sample]))

    def test_half_goal_only_handicaps_and_unicode_team_aliases(self):
        sample = event()
        sample["markets"][-1]["line"] = "-1.25"
        fixture = moneyline_fixture_from_event(sample, now=NOW)
        selected, warnings = discover_popular_handicaps([sample], [fixture])
        self.assertFalse(selected)
        self.assertTrue(warnings)
        quote = HandicapQuote(Decimal("-1"), Decimal("1"), Decimal("2"), Decimal("2"), NOW)
        self.assertIsNone(quote.fair_probability("home", Decimal("-1")))
        for left, right in [("Club Atlético de Madrid", "Atletico Madrid"), ("SSC Napoli", "Napoli"),
                            ("Club Brugge KV", "Club Brugge"), ("Sabah FK", "Sabah")]:
            self.assertEqual(normalize_team(left), normalize_team(right))

    def test_dynamic_ucl_series_and_event_pagination(self):
        offsets = []
        def handler(request):
            if request.url.path == "/sports":
                return httpx.Response(200, json=[dict(sport="epl", series="one"), dict(sport="ucl", series="two")])
            self.assertEqual(request.url.params["series_id"], "two")
            offsets.append(int(request.url.params["offset"]))
            return httpx.Response(200, json=[event()] * 100 if offsets[-1] == 0 else [event("ucl-last")])
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            series, events = PolymarketPublicClient(client=http).list_competition_events("ucl")
        self.assertEqual(series, "two")
        self.assertEqual(offsets, [0, 100])
        self.assertEqual(len(events), 101)


class ChampionsLeagueOddsTests(unittest.TestCase):
    def test_fair_yes_no_handicap_and_snapshots_end_to_end(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            self.assertIn("/soccer_uefa_champs_league/odds", request.url.path)
            self.assertEqual(request.url.params["bookmakers"], "pinnacle")
            return httpx.Response(200, json=[odds_event()], headers={"x-requests-remaining": "498"})
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(handler)) as http:
            archive = Path(directory) / "odds.sqlite3"
            provider = TheOddsApiPinnacleProvider("test-secret", sport_key="soccer_uefa_champs_league", client=http, snapshot_path=archive)
            client = FakeClient()
            reports, warnings = build_market_reports(client, competitions=("ucl",), providers={"ucl": provider}, now=NOW)
            self.assertFalse(warnings)
            fixture = reports[0].fixtures[0]
            self.assertEqual(fixture.outcomes[0].pinnacle_fair_probability, Decimal(".5"))
            self.assertEqual(fixture.outcomes[1].pinnacle_fair_probability, Decimal(".5"))
            self.assertEqual(fixture.outcomes[0].price_gap, Decimal(".1"))
            self.assertEqual(fixture.outcomes[2].pinnacle_fair_probability, Decimal(".25"))
            self.assertEqual(fixture.handicap.outcomes[0].pinnacle_fair_probability, Decimal(".5"))
            self.assertEqual(fixture.handicap.outcomes[0].price_gap, Decimal(".1"))
            build_market_reports(client, competitions=("ucl",), providers={"ucl": provider}, now=NOW)
            self.assertEqual(len(calls), 1)
            with closing(sqlite3.connect(archive)) as db:
                rows = db.execute("SELECT sport_key, quotes_json FROM odds_snapshots").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "soccer_uefa_champs_league")
            self.assertNotIn("test-secret", rows[0][1])
            self.assertIn("captured_at", rows[0][1])

    def test_alternate_endpoint_uses_ucl_and_exact_line(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            data = odds_event()
            if "/events/" in request.url.path:
                self.assertEqual(request.url.path, "/v4/sports/soccer_uefa_champs_league/events/odds-ucl/odds")
                data["bookmakers"][0]["markets"] = [data["bookmakers"][0]["markets"][1]]
                data["bookmakers"][0]["markets"][0]["key"] = "alternate_spreads"
                return httpx.Response(200, json=data)
            return httpx.Response(200, json=[odds_event(spread=False)])
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            provider = TheOddsApiPinnacleProvider("key", sport_key="soccer_uefa_champs_league", client=http)
            reports, _ = build_market_reports(FakeClient(), competitions=("ucl",), providers={"ucl": provider}, now=NOW)
        self.assertEqual(len(calls), 2)
        self.assertEqual(reports[0].fixtures[0].handicap.outcomes[0].pinnacle_fair_probability, Decimal(".5"))

    def test_cross_competition_quotes_and_ambiguous_matches_are_not_used(self):
        fixture = moneyline_fixture_from_event(event(), now=NOW)
        for payload in [[odds_event("soccer_epl")], [odds_event(), odds_event()]]:
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as http:
                provider = TheOddsApiPinnacleProvider("key", sport_key="soccer_uefa_champs_league", client=http)
                provider.refresh_if_due()
                self.assertIsNone(provider.quote_for(fixture))
        with self.assertRaisesRegex(ConfigError, "competition"):
            build_premier_league_report(FakeClient(), competition="ucl", team_filter={"club 0"},
                                       odds_provider=TheOddsApiPinnacleProvider("key"), now=NOW)

    def test_odds_failure_keeps_books_and_redacts_key(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"message": "bad test-secret"}))) as http:
            provider = TheOddsApiPinnacleProvider("test-secret", sport_key="soccer_uefa_champs_league", client=http)
            reports, warnings = build_market_reports(FakeClient(), competitions=("ucl",), providers={"ucl": provider}, now=NOW)
        self.assertFalse(warnings)
        report = reports[0]
        self.assertTrue(report.fixtures[0].outcomes[0].metrics)
        self.assertIsNone(report.fixtures[0].outcomes[0].pinnacle_fair_probability)
        self.assertIn("refresh unavailable", str(report.warnings))
        self.assertNotIn("test-secret", render_terminal(report))

    def test_shared_budget_reserves_both_bulk_calls_and_bounds_alternates(self):
        budget = OddsRequestBudget(competitions=2)
        budget.reserve(alternate=False)
        for _ in range(8):
            budget.reserve(alternate=True)
        budget.reserve(alternate=False)  # EPL alternates cannot consume UCL bulk headroom.
        for alternate in [True, False]:
            with self.assertRaises(ApiError):
                budget.reserve(alternate=alternate)
        budget.started -= 72001
        budget.reserve(alternate=False)
        self.assertEqual(budget.used, {"bulk": 2, "alternate": 0})

    def test_remaining_account_credits_guard_requests(self):
        budget = OddsRequestBudget(competitions=2)
        budget.remaining = 1
        with self.assertRaisesRegex(ApiError, "Insufficient"):
            budget.reserve(alternate=False)
        budget.reserve(alternate=True)
        self.assertEqual(budget.remaining, 0)

    def test_quota_reset_is_discovered_with_a_free_request(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, json=[] if request.url.path == "/v4/sports" else [odds_event()],
                                  headers={"x-requests-remaining": "100"})
        budget = OddsRequestBudget()
        budget.remaining = 0
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            provider = TheOddsApiPinnacleProvider("key", sport_key="soccer_uefa_champs_league", client=http, budget=budget)
            self.assertTrue(provider.refresh_if_due())
        self.assertEqual(paths, ["/v4/sports", "/v4/sports/soccer_uefa_champs_league/odds"])

    def test_alternate_allocation_does_not_spend_when_disabled(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[odds_event(spread=False)]))) as http:
            provider = TheOddsApiPinnacleProvider("key", sport_key="soccer_uefa_champs_league", client=http, max_alternate_requests=0)
            reports, _ = build_market_reports(FakeClient(), competitions=("ucl",), providers={"ucl": provider}, now=NOW)
        self.assertIn("allocation reached", str(reports[0].warnings))
        self.assertEqual(provider.budget.used["alternate"], 0)
        self.assertIsNone(reports[0].fixtures[0].handicap.outcomes[0].pinnacle_fair_probability)


if __name__ == "__main__":
    unittest.main()
