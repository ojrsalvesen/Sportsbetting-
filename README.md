# Premier League market monitor

A read-only terminal monitor for upcoming Premier League fixtures on Polymarket. It fetches the public order books for both the `YES` and `NO` token of every home, draw, and away outcome.

For each token it prints the best bid, best ask, spread, visible bid/ask depth within two cents, and the estimated VWAP for a purchase of up to $5. It can optionally add Pinnacle's three-way prices and margin-normalized probabilities through The Odds API's free tier.

The program never connects a wallet, signs an order, or places a bet.

It also includes a separate personal analytics command. That command reads a public
Polymarket profile's EPL activity, stores it in a local SQLite ledger, and compares
BUY fills and results with rolling and post-match expected goals as descriptive context.

## Install

From PowerShell in this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Python 3.11 or newer is required.

## Run

One terminal snapshot:

```powershell
.\.venv\Scripts\premier-league-report.exe
```

Refresh the Polymarket books every 60 seconds and redraw the terminal:

```powershell
.\.venv\Scripts\premier-league-report.exe --watch
```

Use `--no-clear` with `--watch` to retain earlier snapshots. Run with `--help` for all settings.

## Optional free Pinnacle comparison

Create a free The Odds API account and run:

```powershell
.\.venv\Scripts\premier-league-report.exe --pinnacle-api
```

If `THE_ODDS_API_KEY` is not set, the program asks for it with hidden input. In watch mode Polymarket still refreshes every minute, while Pinnacle refreshes no more than once every 90 minutes to remain within a 500-request monthly allowance. The remaining request count is printed when the provider returns it.

Pinnacle coverage is supplied by the third-party provider and is not guaranteed for every fixture or account tier. Without `--pinnacle-api`, the monitor remains fully useful and shows Polymarket data only.

## Columns

- `Tok`: the `YES` or `NO` outcome token.
- `Bid` / `Ask`: the best visible order-book prices.
- `Spr`: ask minus bid.
- `Bid$2c` / `Ask$2c`: visible USD notional within $0.02 of the best price.
- `BuyVWAP`: estimated average ask price when walking the visible book for the
  configured stake (default $5).
- `PinFair`: Pinnacle's no-vig probability. For `NO`, this is one minus the corresponding outcome probability.
- `Gap`: `PinFair` minus `BuyVWAP`. It is `n/a` if the full stake is not visible
  on the ask side, and it is a benchmark comparison rather than a profit forecast.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The public network contract test is opt-in:

```powershell
$env:PMR_RUN_NETWORK_TESTS='1'
.\.venv\Scripts\python.exe -m unittest tests.test_network_contract -v
```

## Personal bet history and xG

The analytics command needs your public Polymarket profile address—the `0x...`
address visible on your profile. It never needs a private key, wallet signature, or
Polymarket API credential.

On the first run:

```powershell
.\.venv\Scripts\bet-analytics.exe --user 0xYOUR_PUBLIC_PROFILE_ADDRESS
```

The command automatically:

1. Downloads this season's EPL trade activity and closed positions from
   Polymarket's public Data API.
2. Stores new records idempotently in `data/bet_history.sqlite3`.
3. Fetches completed EPL match xG for the current and previous seasons from
   Understat's free public league endpoint.
4. Prints entry price, outcome, hypothetical hold-to-settlement P/L,
   closed-position realized P/L, rolling xGF/xGA, and post-match xG.

Once a profile has been stored, later runs can omit `--user`:

```powershell
.\.venv\Scripts\bet-analytics.exe
```

Keep it running and synchronize automatically every six hours:

```powershell
.\.venv\Scripts\bet-analytics.exe --watch
```

Because every run backfills and deduplicates the season, the process does not need
to be online when a bet is placed. Use `--refresh-hours 12` to change the interval;
the enforced minimum is one hour.

Use cached data without any network requests:

```powershell
.\.venv\Scripts\bet-analytics.exe --offline
```

Use `--season 2025` for the 2025/26 season. A season is identified by its starting
year. Only EPL event slugs are imported.

Rolling xG uses up to each team's previous five EPL matches available before the
fixture, including the prior season when available, so future-match data cannot
leak into the context. It is descriptive and is not treated as your probability
estimate or as a betting model. Understat access is unofficial and may change; it
is isolated behind a provider adapter so it can be replaced without changing the
ledger.

All EPL fills remain in the audit tables, but automatic match results and
hold-to-settlement P/L currently cover full-time 1X2 selections only. Futures,
spreads, totals, and both-teams-to-score markets remain `PENDING` in that
hypothetical analysis; closed-position realized P/L still comes from Polymarket.

## Analyze the ledger with pandas

Install the optional notebook tools:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[notebook]"
```

Keep the tracked notebook as a clean template and work in a private local copy:

```powershell
Copy-Item .\notebooks\bet_history_analysis.ipynb .\notebooks\bet_history_analysis.local.ipynb
.\.venv\Scripts\jupyter-lab.exe .\notebooks\bet_history_analysis.local.ipynb
```

You can use the project `.venv`, or install the project into an existing notebook
environment and keep using that kernel:

```powershell
PATH_TO_YOUR_PYTHON.exe -m pip install -e ".[notebook]"
```

The notebook loads `data/bet_history.sqlite3` into four pandas DataFrames:
enriched BUY bets (`bets`), all BUY/SELL fills (`trades`), Polymarket closed
positions (`closed_positions`), and Understat matches (`xg_matches`). Its starter
analysis aggregates repeated fills by selection, weights entry prices by shares,
and includes cumulative hold-to-settlement P/L and xG data. Rerun `bet-analytics`
before opening the notebook whenever you want to synchronize newly placed bets.

The live monitor's `PinFair` value is a margin-normalized Pinnacle implied
probability and is used only as a benchmark. Pinnacle quotes are not currently
written to the analytics ledger, so the notebook cannot reconstruct the exact
benchmark that was visible when an older bet was placed.

Files ending in `.local.ipynb`, notebook checkpoints, and the entire `data/`
directory are ignored by Git because executed cells can reveal your public profile
address and betting history.
