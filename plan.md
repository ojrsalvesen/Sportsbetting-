# Product plan

## Current product

The product is a read-only terminal monitor for all Premier League fixtures kicking off within the next seven days. It discovers Polymarket's active full-time 1X2 markets and fetches six moneyline books per fixture (`YES` and `NO` for home, draw, and away), plus both tokens for one main full-match handicap.

Each refresh shows best bids and asks, spread, near-touch depth, and the visible-book VWAP for a configurable stake capped at $5. The default is a single snapshot; watch mode clears and redraws the terminal every 60 seconds.

Pinnacle comparison is optional. With `--pinnacle-api`, the program requests Pinnacle 1X2 and featured-spread odds through The Odds API, then queries per-event alternate spreads when the favourite's selected Polymarket handicap needs another line. It removes the appropriate three-way or two-way margin and compares the resulting fair probability with each Polymarket token's executable VWAP. Handicap comparisons require an exact line match. The integration is constrained to a 20-hour minimum refresh so the 12-credit worst case remains below roughly 500 credits per 31 days.

There is no trading path, wallet access, persistence, file-report output, manual odds CSV, or paid fallback.

## Verification plan

1. Fixture parsing must accept only upcoming three-way full-time moneyline events, consistently map home/draw/away, and retain every kickoff within the next seven days without a gap or match-count limit.
2. Every report must request both token sides for every moneyline outcome and the selected handicap, flagging missing or mismatched books without hiding the rest of the fixture.
3. Metric tests must cover unsorted books, empty sides, spread, depth windows, partial fills, and $5 VWAP.
4. Pinnacle tests must mock featured and alternate-spread response shapes, match normalized team names, kickoff times and handicap lines, normalize the overround, redact the key, expose quota headers, and reject refresh periods below 20 hours.
5. A public opt-in contract test must verify current Polymarket fixture discovery and all requested books for at least one fixture.

## Possible next improvements

- Add terminal filters for team, kickoff window, maximum spread, and minimum depth.
- Highlight stale books and unusually wide spreads more clearly.
- Add a compact view that shows only the three `YES` lines per fixture while preserving an option to inspect all six books.
- Cache the last Pinnacle response on disk only if users need odds to survive process restarts; encrypting the API key is unnecessary because it should never be cached.

Automated order placement remains out of scope unless it is designed later as a separate, explicitly reviewed project.
