# Product plan

## Current product

The product is a read-only terminal monitor for all Premier League fixtures kicking off within the next seven days. It discovers Polymarket's active full-time 1X2 markets and fetches six moneyline books per fixture (`YES` and `NO` for home, draw, and away), plus both tokens for one main full-match handicap.

Each run shows best bids and asks, spread, near-touch depth, and the visible-book VWAP for a configurable stake capped at $5, then exits. A separate section covers listed upcoming Champions League matches involving current EPL teams.

Pinnacle comparison is optional. With `--pinnacle-api`, the program requests Pinnacle 1X2 and featured-spread odds through The Odds API, then queries per-event alternate spreads when the favourite's selected Polymarket handicap needs another line. It removes the appropriate three-way or two-way margin and compares the resulting fair probability with each Polymarket token's executable VWAP. Handicap comparisons require an exact line match. Both competitions share a 12-credit budget per invocation. Each new CLI invocation spends credits again; the provider's in-process 20-hour refresh guard does not enforce an account-wide monthly limit.

There is no trading path or wallet access. The optional odds archive and personal
bet ledger use local SQLite databases. The analytics notebook exports charts and
CSV audit tables to ignored local storage. `bet-analytics --watch` refreshes the
public trade history and xG on a default six-hour interval.

## Verification plan

1. Fixture parsing must accept only upcoming three-way full-time moneyline events, consistently map home/draw/away, and retain every kickoff within the next seven days without a gap or match-count limit.
2. Every report must request both token sides for every moneyline outcome and the selected handicap, flagging missing or mismatched books without hiding the rest of the fixture.
3. Metric tests must cover unsorted books, empty sides, spread, depth windows, partial fills, and $5 VWAP.
4. Pinnacle tests must mock featured and alternate-spread response shapes, match normalized team names, kickoff times and handicap lines, normalize the overround, redact the key, expose quota headers, and enforce the per-run request budget and the provider's in-process refresh guard.
5. A public opt-in contract test must verify current Polymarket fixture discovery and all requested books for at least one fixture.

## Possible next improvements

- Add terminal filters for team, kickoff window, maximum spread, and minimum depth.
- Highlight stale books and unusually wide spreads more clearly.
- Add a compact view that shows only the three `YES` lines per fixture while preserving an option to inspect all six books.
- Join archived Pinnacle quotes to historical fills only when timestamps and exact market lines can be matched reliably; never store the API key.

Automated order placement remains out of scope unless it is designed later as a separate, explicitly reviewed project.
