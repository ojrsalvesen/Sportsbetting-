# Product plan

## Current product

The product is a read-only terminal monitor for upcoming Premier League fixtures. It discovers Polymarket's active full-time 1X2 markets and fetches six public books per fixture: `YES` and `NO` for home, draw, and away.

Each refresh shows best bids and asks, spread, near-touch depth, and the visible-book VWAP for a configurable stake capped at $5. The default is a single snapshot; watch mode clears and redraws the terminal every 60 seconds.

Pinnacle comparison is optional. With `--pinnacle-api`, the program requests Pinnacle 1X2 odds through The Odds API, removes the three-way margin, and compares the resulting fair probability with each Polymarket token's best ask. The integration is constrained to a 90-minute minimum refresh so a continuously running monitor stays within roughly 500 requests per 31 days.

There is no trading path, wallet access, persistence, file-report output, manual odds CSV, or paid fallback.

## Verification plan

1. Fixture parsing must accept only upcoming three-way full-time moneyline events and consistently map home, draw, and away.
2. Every report must request both token sides for every outcome and flag missing or mismatched books without hiding the rest of the fixture.
3. Metric tests must cover unsorted books, empty sides, spread, depth windows, partial fills, and $5 VWAP.
4. Pinnacle tests must mock the provider's documented response shape, match normalized team names and kickoff times, normalize the overround, redact the key, expose quota headers, and reject refresh periods below 90 minutes.
5. A public opt-in contract test must verify current Polymarket fixture discovery and all six books for at least one fixture.

## Possible next improvements

- Add terminal filters for team, kickoff window, maximum spread, and minimum depth.
- Highlight stale books and unusually wide spreads more clearly.
- Add a compact view that shows only the three `YES` lines per fixture while preserving an option to inspect all six books.
- Cache the last Pinnacle response on disk only if users need odds to survive process restarts; encrypting the API key is unnecessary because it should never be cached.

Automated order placement remains out of scope unless it is designed later as a separate, explicitly reviewed project.
