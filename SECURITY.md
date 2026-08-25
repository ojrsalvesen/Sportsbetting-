# Security

This project is read-only. It uses public Polymarket endpoints and has no wallet, signing, trading, database, or order-placement code.

The optional `THE_ODDS_API_KEY` is the only credential. Do not commit it to `.env.example`, source code, screenshots, logs, or shell history. The safest normal use is `--pinnacle-api` without an environment variable: the terminal then collects the key through a hidden prompt and keeps it only in process memory.

If automation requires an environment variable, inject it only for the process and keep local `.env` variants out of version control. Error messages and object representations redact the key, but upstream tooling may still record environment variables.

No odds or betting feed guarantees correctness or freshness. Verify prices and market resolution rules independently before using the report for a financial decision.
