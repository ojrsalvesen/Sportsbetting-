# Premier League market monitor

A read-only terminal monitor for all Premier League fixtures kicking off within the next seven days and all listed upcoming Champions League matches involving current EPL teams. It fetches the public order books for both the `YES` and `NO` token of every home, draw, and away outcome, plus one full-match half-goal handicap per fixture. The competitions appear in separate sections.

For each token it prints the best bid, best ask, spread, visible bid/ask depth within two cents, and the estimated VWAP for a purchase of up to $5. It can optionally add Pinnacle's three-way and featured-handicap prices, converted to margin-normalized probabilities through The Odds API.

The program never connects a wallet, signs an order, or places a bet.

It also includes a separate personal analytics command. That command reads a public
Polymarket profile's EPL and Champions League activity, stores it in a local SQLite
ledger, and compares BUY fills and results with post-match expected goals when a
provider has matching fixture data.

This is a personal research and data-engineering project. It does not claim a
profitable betting strategy. Post-match xG diagnostics use information that was
not available when a bet was placed; they cannot demonstrate an entry-time edge.
Personal results are kept outside the public repository.

## Install

From PowerShell in this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Python 3.11 or newer is required.

On macOS/Linux, use `.venv/bin/python`, `.venv/bin/premier-league-report`, and
`.venv/bin/bet-analytics` in place of the Windows executable paths below.
`.env.example` documents environment variables; the CLI does not automatically
load `.env` files.

## Run

One terminal snapshot:

```powershell
.\.venv\Scripts\premier-league-report.exe
```

Each run fetches one snapshot, prints it, and exits. Run the command again when
you want updated prices. Run with `--help` for all settings.

## Optional free Pinnacle comparison

Create a free The Odds API account and run:

```powershell
.\.venv\Scripts\premier-league-report.exe --pinnacle-api
```

If `THE_ODDS_API_KEY` is not set, the program asks for it with hidden input. Each run fetches Polymarket and Pinnacle prices once, prints the comparison, and exits. Both competitions share a conservative 12-credit request budget per run: four credits reserved for the two `h2h,spreads` calls, plus eight exact alternate-handicap lookups (up to four per competition). Single-competition mode reserves two plus ten. Exhausted requests are skipped with a warning; no unmatched line is substituted. Running the command again makes new requests and uses additional credits; account-wide remaining-credit headers also guard requests. Quotes show their bookmaker update time.

The report first discovers all upcoming EPL moneyline fixtures, then retains every kickoff after the report time and up to and including seven days later (a rolling 168-hour UTC window). Gaps between fixtures do not end the selection, and there is no ten-match cap, so midweek and weekend fixtures can appear together. The Pinnacle request is bounded to the same selected kickoff range.

For the handicap row, the report identifies the favourite from Pinnacle's 1X2 prices, falling back to Polymarket's 1X2 probability when necessary. It then selects only that team's negative full-match handicaps and chooses the highest-volume line, using liquidity as a tiebreaker. If Pinnacle's featured spread is a different line, the report requests `alternate_spreads` for that event and looks up the exact selected line. A Pinnacle probability and gap are never shown against a different handicap.

Pinnacle coverage is supplied by the third-party provider and is not guaranteed for every fixture or account tier. Without `--pinnacle-api`, the monitor remains fully useful and shows Polymarket data only.

### Champions League scope

Both sections are enabled by default. To select one explicitly:

```powershell
.\.venv\Scripts\premier-league-report.exe --competitions ucl --pinnacle-api
.\.venv\Scripts\premier-league-report.exe --competitions epl --pinnacle-api
```

Competition metadata and event pagination come from Polymarket's public Gamma API.
The current EPL team set is derived from this season's dated EPL moneyline fixtures,
not the historical `/teams` directory (which also includes relegated teams). If the
listed fixtures do not establish exactly 20 teams, the UCL section is skipped with
an explicit warning rather than silently using an incomplete or outdated roster.
All listed upcoming UCL fixtures with at least one of those teams are retained;
non-English-only matches are excluded before fetching their books. This is market
coverage, not a promise of a complete future tournament schedule.

UCL prices use The Odds API's `soccer_uefa_champs_league`, with a separate cache
from `soccer_epl`. Team aliases and kickoff times must match uniquely. Moneyline
and spread descriptions must explicitly specify 90-minute settlement; qualification,
extra-time-inclusive and half-time markets are excluded. Full 1X2 margin removal
is `p_i = (1/odds_i) / sum(1/odds_j)`; NO uses `1-p_i`. For half-goal handicaps,
the same calculation uses the two opposing prices at the exact line. Whole/quarter
handicaps are not treated as binary probabilities. `Gap` compares the fair
probability with full-stake ask VWAP before fees. Missing or failed odds remain
`n/a`; Polymarket books and the other competition can still render.

Successful parsed odds refreshes are appended to `data/odds_snapshots.sqlite3`
(override with `--odds-snapshots`). The archive stores fetch time, bookmaker update
times, competition, event IDs, teams, lines and prices, never API keys or request
URLs. Quotes outside the selected fixture scope are not archived. The archive does
not yet join quotes to historical bets. Bet-history includes EPL and UCL; xG analytics
currently have Understat coverage for EPL only.

API references: [Polymarket sports metadata](https://docs.polymarket.com/api-reference/sports/get-sports-metadata-information),
[The Odds API](https://the-odds-api.com/liveapi/guides/v4/).

## Columns

- `Tok`: the `YES` or `NO` moneyline token; `HCP` identifies a handicap outcome token.
- `Bid` / `Ask`: the best visible order-book prices.
- `Spr`: ask minus bid.
- `Bid$2c` / `Ask$2c`: visible USD notional within $0.02 of the best price.
- `BuyVWAP`: estimated average ask price when walking the visible book for the
  configured stake (default $5).
- `PinFair`: Pinnacle's no-vig probability. For `NO`, this is one minus the corresponding outcome probability. Handicap values require an exact line match.
- `Gap`: `PinFair` minus `BuyVWAP`. It is `n/a` if the full stake is not visible
  on the ask side, and it is a benchmark comparison rather than a profit forecast.

## Test

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[notebook]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The notebook extra is required for the pandas and plot tests. GitHub Actions runs
the offline suite on Windows and Linux with Python 3.11 and 3.13.

The public network contract test is opt-in:

```powershell
$env:PMR_RUN_NETWORK_TESTS='1'
.\.venv\Scripts\python.exe -m unittest tests.test_network_contract -v
Remove-Item Env:PMR_RUN_NETWORK_TESTS
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

1. Downloads this season's EPL and Champions League trade activity and closed positions from
   Polymarket's public Data API.
2. Stores new records idempotently in `data/bet_history.sqlite3`.
3. Fetches the selected season's completed EPL match xG from Understat's free
   public league endpoint, then retains only fixtures represented by your BUY fills.
   Champions League fills are included in the ledger and settlement analysis; the
   Understat adapter currently supplies EPL xG only, so UCL rows show `n/a` for xG.
4. Prints entry price, outcome, hypothetical hold-to-settlement P/L,
   closed-position realized P/L, and post-match fixture xG.

Once a profile has been stored, later runs can omit `--user`:

```powershell
.\.venv\Scripts\bet-analytics.exe
```

Run from the project directory: the default database path is relative to your
working directory. Running elsewhere can create a second, empty ledger and ask
for your profile again. Use `--database` with an absolute path when needed.

Each report shows `SYNC COMPLETE`, `INCOMPLETE SYNC`, or `OFFLINE`, plus the latest
stored trade time and the database path. A failed online refresh can still show
cached results, but a single run now exits with code 2 if any source failed.
Watch mode reports the failure and retries at its next interval. A recent trade
timestamp alone is not proof that all settlements or xG have refreshed.

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
year. The trade ledger is restricted to EPL and Champions League event slugs and trades placed between
July 1 of the starting year and July 1 of the next year. The xG table exposed to
the report and notebook is restricted further: one completed match for each
fixture represented by BUY fills. The Understat league endpoint itself returns
the league schedule, so filtering happens immediately after download and before
new xG rows are stored.

Fixture xG is descriptive evidence about the chances created in the match. It is
not a probability estimate, a betting model, or a score of historical price quality.
Understat access is unofficial and may change; it is isolated behind a provider
adapter so it can be replaced without changing the ledger.

All EPL and Champions League fills remain in the audit tables. For resolved fixture markets, the
analytics caches and uses Polymarket's official winning outcome, including BTTS,
spreads, and totals. When that metadata is unavailable offline, final scores
provide a fallback for full-time 1X2, full-match BTTS, and half-goal spreads.
Whole/quarter-goal spreads and period-specific markets require official outcomes.
A market remains `PENDING` when it is unresolved or its rules
cannot be interpreted safely. Match xG is descriptive only and never determines
settlement; closed-position realized P/L still comes from Polymarket.

## Analyze the ledger with pandas

Install the optional notebook tools:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[notebook]"
```

Keep the tracked notebook as a clean template and work in a private local copy:

```powershell
Copy-Item .\notebooks\bet_history_analysis.ipynb .\notebooks\bet_history_analysis.clean.local.ipynb
.\.venv\Scripts\jupyter-lab.exe .\notebooks\bet_history_analysis.clean.local.ipynb
```

Copy the template only when creating a new local notebook; do not overwrite an
existing analysis to refresh its data. In Jupyter, select the project environment,
restart the kernel, and run all cells after a successful `bet-analytics` sync.
For a command-line refresh of an existing local notebook:

```powershell
.\.venv\Scripts\bet-analytics.exe
if ($LASTEXITCODE -ne 0) { throw 'Sync incomplete; inspect the warnings before refreshing plots.' }
.\.venv\Scripts\python.exe -m jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=600 notebooks\bet_history_analysis.clean.local.ipynb
```

If a notebook is already open, reload it after external execution to see the saved
outputs. Use the same database, season, and profile in the CLI and notebook.

You can use the project `.venv`, or install the project into an existing notebook
environment and keep using that kernel:

```powershell
PATH_TO_YOUR_PYTHON.exe -m pip install -e ".[notebook]"
```

The notebook loads `data/bet_history.sqlite3` into four season-scoped pandas
DataFrames: enriched BUY fills (`bets`), all BUY/SELL fills (`trades`), Polymarket
closed positions (`closed_positions`), and only the completed xG fixtures represented
by BUY fills (`xg_matches`). The analysis validates its scope,
aggregates repeated fills by selection, weights entry prices by shares, keeps
hypothetical hold P/L separate from realized closed-position P/L, and presents xG
only as a match diagnostic. Rerun `bet-analytics` before opening the notebook
whenever you want to synchronize newly placed bets.

The live monitor's `PinFair` value is a margin-normalized Pinnacle implied
probability and is used only as a benchmark. Pinnacle quotes are not currently
written to the analytics ledger, so the notebook cannot reconstruct the exact
benchmark that was visible when an older bet was placed.

Files ending in `.local.ipynb`, notebook checkpoints, and the entire `data/`
directory are ignored by Git because executed cells can reveal your public profile
address and betting history.

## Performance plots

The local analysis notebook opens as a plots-only view with collapsed preparation
cells and no automatic DataFrame printouts. The template prints the cache path and
latest stored trade time. It shows reconstructed realized EPL/UCL P/L, actual goal difference
versus xG difference, cumulative xG for/against with net xG difference, and a
per-match goals/xG comparison. PNGs and the underlying CSV audit tables are
exported to the ignored `data/performance_plots/` directory when the cells run.
The underlying DataFrames remain available for manual inspection, including
`bets`, `trades`, `selections`, `scope_checks`, `directional_matches`, `pnl_daily`
and `pnl_events`. Scope checks still execute silently and raise on invalid data.

The xG curves give one equal-weight observation to each completed fixture/team
direction. Fragmented BUY fills and multiple handicap lines in the same direction
do not repeat the match. Outcome overperformance is `(GF - GA) - (xGF - xGA)`.
Draw, BTTS, totals and futures have no signed team direction and are excluded from
these curves. Live fills are flagged in the audit table; the xG remains full-match.

The P/L curve uses recorded BUY costs and SELL receipts with moving-average cost
basis, then settles only the remaining shares using official Polymarket outcomes.
It includes losing holdings even if they have not entered the closed-position API.
Settlement proceeds are recognized once, so redemptions are not added again.
Sales are dated at execution; settlements are assigned to the UTC fixture date,
or the last trade date if later. This is a reconstructed realized P/L curve for the
selected football season, not historical marked-to-market equity or wallet balance.
Remaining unresolved inventory is listed separately; tiny share residuals from
source rounding can remain in that table. SELLs without sufficient imported BUY
history raise an error instead of assuming zero acquisition cost. This reconstruction
assumes positions were acquired and disposed of through the imported BUY/SELL fills:
token transfers, splits/merges, and other non-trade inventory changes are not modeled.
Trade timestamps have second precision; ambiguous ordering within a second can
also prevent exact cost-basis reconstruction.

To rebuild the plots-only view of an existing local notebook (a backup is saved first):

```powershell
.\.venv\Scripts\python.exe scripts\add_performance_section.py --notebook notebooks\bet_history_analysis.clean.local.ipynb
```

Rerun `bet-analytics`, then run all notebook cells to refresh data and plots. The
plot implementation is reusable through `polymarket_bot.performance` and requires
the optional notebook dependencies.

### xG return benchmark

Two further plots compare your entry price with a retrospective xG benchmark:
per-selection benchmark ROI (bar colour indicates positive/negative ROI; separate
symbols indicate wins/losses), and cumulative benchmark versus observed
hold-to-settlement P/L for exactly the same selections.

Independent Poisson score distributions use completed Understat home/away xG.
The score grid bounds omitted probability below `1e-12` before renormalising.
Supported selections are full-time home/away/draw YES or NO, full-match BTTS
YES/NO, and half-goal team handicaps, including the opposing team selection.
Whole/quarter-goal handicaps, futures and ambiguous markets are excluded.
Only officially settled selections with completed scores/xG and pre-kickoff BUY
fills qualify. Live fills are excluded individually, so pre-match fills of a
mixed selection remain eligible. Fragmented fills are summed by selection token;
different lines stay separate. Score-rule/official-outcome mismatches are audited
as exclusions, and conflicting included source data raises an error.

For purchased shares `Q`, recorded purchase cost `C`, and xG selection probability
`p`, break-even probability is `C/Q`, xG edge is `p-C/Q`, benchmark P/L is
`Q*p-C`, and benchmark ROI is `(Q*p-C)/C`. Observed hold P/L is `Q*won-C`;
outcome deviation is observed minus benchmark P/L. Recorded cash cost is used
as supplied (including fees already embedded in it); no additional fee estimate
is added. This compares with your actual Polymarket entry price, not a reconstructed
bookmaker quote. It does not attempt to quantify your intuition before the match.

Both new P/L curves assume the same eligible purchased shares were held until
settlement, regardless of subsequent sales. They are distinct from the existing
realized P/L chart. Positive benchmark ROI means the price was favourable against
the chances subsequently created, not proof that the bet was good beforehand.
The outcome gap is descriptive, not a significance test of luck or skill; selections
on the same match are correlated. Daily totals are grouped by UTC match date.
No fitted forecasting system, shot downloads or new dependencies are needed.

The notebook keeps `xg_benchmark_selections`, `xg_benchmark_daily`,
`xg_benchmark_fill_audit` and `xg_benchmark_exclusions` available without displaying
them. Matching `xg_benchmark_*.csv` files and the two PNGs are exported alongside
the existing plots in `data/performance_plots/`.

## Publishing

Publish source code and the output-free notebook template. Keep `data/`, local
notebooks, environment files, API keys, and private screenshots out of commits.
See [SECURITY.md](SECURITY.md) for the privacy boundary. Deleting a previously
committed file does not remove it from Git history.

The repository currently has no license file; choose one before presenting it as
an open-source release.
