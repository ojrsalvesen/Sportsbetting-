# Security

This project is read-only with respect to Polymarket. It uses public endpoints
and has no wallet-signing or order-placement code. It does write local SQLite
databases, notebook outputs, charts, and CSV audit files.

The optional `THE_ODDS_API_KEY` is the only credential. Do not commit it to `.env.example`, source code, screenshots, logs, or shell history. The safest normal use is `--pinnacle-api` without an environment variable: the terminal then collects the key through a hidden prompt and keeps it only in process memory.

If automation requires an environment variable, inject it only for the process and keep local `.env` variants out of version control. Error messages and object representations redact the key, but upstream tooling may still record environment variables.

No odds or betting feed guarantees correctness or freshness. Verify prices and market resolution rules independently before using the report for a financial decision.

The analytics command accepts only a public `0x...` Polymarket profile address. It
does not accept a private key. The address and public trade history are personal
information even though they are publicly queryable, so the SQLite database lives
under the Git-ignored `data/` directory. Do not publish the database unless you
intend to disclose your trading history.

The tracked analysis notebook contains code only. Run and save analysis in an
ignored `notebooks/*.local.ipynb` copy: notebook outputs, exported charts, and
DataFrame previews can disclose the same personal information as the database.
Before sharing any notebook, clear every cell output and check it for addresses,
transaction hashes, and bet details.

Custom SQLite database paths are ignored by extension as well as by the default
`data/` directory. Custom exports outside `data/` need separate review before
staging. CI checks that the public notebook template has no saved outputs or
execution metadata; it is not a general-purpose secret scanner. Review Git history
as well as the working tree before changing repository visibility. Commit author
names and email addresses are also visible in a public repository.
