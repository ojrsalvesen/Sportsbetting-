# Premier League market monitor

A read-only terminal monitor for upcoming Premier League fixtures on Polymarket. It fetches the public order books for both the `YES` and `NO` token of every home, draw, and away outcome.

For each token it prints the best bid, best ask, spread, visible bid/ask depth within two cents, and the estimated VWAP for a purchase of up to $5. It can optionally add Pinnacle's three-way prices and margin-normalized probabilities through The Odds API's free tier.

The program never connects a wallet, signs an order, or places a bet.

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
- `$5VWAP`: estimated average ask price when walking the visible book for the selected stake.
- `PinFair`: Pinnacle's no-vig probability. For `NO`, this is one minus the corresponding outcome probability.
- `Gap`: `PinFair` minus Polymarket's best ask; it is a comparison, not a profit forecast.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The public network contract test is opt-in:

```powershell
$env:PMR_RUN_NETWORK_TESTS='1'
.\.venv\Scripts\python.exe -m unittest tests.test_network_contract -v
```
