# Hyperliquid Usage Guide

This guide covers day-to-day usage of the Hyperliquid integration.

## Prerequisites

- `config/accounts.toml` has a Hyperliquid account, for example:
  - `[accounts.hl_main]`
  - `source = "hyperliquid"`
  - `account_id = "0x..."`
- `.venv` is active and project is installed in editable mode.
- `config/app.toml` points to your SQLite DB path (default: `data/trade_journal.sqlite`).

Notes:

- `account_id` is canonicalized to lowercase.
- Current journal integration is read-only (`/info` endpoint). No private key is required.

## Quick Sanity Check

Run:

```bash
.venv/bin/python -m trade_journal.sanity_hyperliquid --account hl_main --lookback-hours 24
```

Expected when no trades exist yet:

- `snapshot_equity` has a value
- `recent_fill_count` is `0`

JSON output variant:

```bash
.venv/bin/python -m trade_journal.sanity_hyperliquid --account hl_main --lookback-hours 24 --json
```

## Sync Commands

Real sync (writes to DB):

```bash
.venv/bin/python -m trade_journal.sync_api --account hl_main --max-pages 20
```

Dry-run sync (Hyperliquid only; no DB writes/checkpoint updates):

```bash
.venv/bin/python -m trade_journal.sync_api --account hl_main --max-pages 20 --dry-run
```

## Debug APIs (Web App)

Open positions:

```bash
curl "http://127.0.0.1:8010/api/account/open-positions?source=hyperliquid&account_id=0x..."
```

Sync checkpoint state (defaults to current account context):

```bash
curl "http://127.0.0.1:8010/api/sync-state"
```

Sync checkpoint state with explicit filters:

```bash
curl "http://127.0.0.1:8010/api/sync-state?source=hyperliquid&account_id=0x..."
curl "http://127.0.0.1:8010/api/sync-state?source=hyperliquid&account_id=0x...&endpoint_prefix=fills:"
```

## What To Expect Before First Fill

- `account_snapshot` rows should appear.
- `fills` can remain `0` until the first execution.
- Dashboard open positions and `/api/account/open-positions` should still work from snapshots.

## Fill Verification Checkpoints

Use this section as a running log: keep one pre-fill baseline and append post-fill snapshots after the first executions.

### Command Sequence (use exactly)

```bash
.venv/bin/python -m trade_journal.sync_api --account hl_main --max-pages 20
curl -s "http://127.0.0.1:8010/api/sync-state?source=hyperliquid&account_id=0x371b6d542a2471c1e8f38495e0dea578d00a377c" | jq
curl -s -w "\n%{http_code}" "http://127.0.0.1:8010/api/account/open-positions?source=hyperliquid&account_id=0x371b6d542a2471c1e8f38495e0dea578d00a377c" | { read -r body; read -r code; echo "$body" | jq; echo "HTTP $code"; }
```

### Baseline (Pre-Fill)

Date: 02/06/2026

Notes: seems good

`sync_api` output:

```
synced_rows 1
accounts 0
fills 0
funding 0
orders 0
historical_pnl 0
equity_history 0
account_snapshot 1
```

`/api/sync-state` output:

```json
{
  "source": "hyperliquid",
  "account_id": "0x371b6d542a2471c1e8f38495e0dea578d00a377c",
  "endpoint_prefix": null,
  "states": [
    {
      "endpoint": "account_snapshot:hyperliquid:0x371b6d542a2471c1e8f38495e0dea578d00a377c",
      "source": "hyperliquid",
      "account_id": "0x371b6d542a2471c1e8f38495e0dea578d00a377c",
      "last_timestamp_ms": 1770401005666,
      "last_id": null,
      "last_success_at": "2026-02-06 18:03:25"
    }
  ]
}
```

`/api/account/open-positions` output:

**NOTE:** this was pointing at Apex when ran

```json
{
  "account": {
    "total_equity": null,
    "available_balance": null,
    "margin_balance": null,
    "timestamp": "2026-02-06T18:45:11.261430+00:00"
  },
  "open_positions": []
}
```

### Post-Fill Checkpoint

Date:

Notes:

`sync_api` output:

```
synced_rows 5
accounts 1
fills 3
funding 0
orders 0
historical_pnl 0
equity_history 0
account_snapshot 1
```

`/api/sync-state` output:

```json
{
  "source": "hyperliquid",
  "account_id": "0x371b6d542a2471c1e8f38495e0dea578d00a377c",
  "endpoint_prefix": null,
  "states": [
    {
      "endpoint": "account_snapshot:hyperliquid:0x371b6d542a2471c1e8f38495e0dea578d00a377c",
      "source": "hyperliquid",
      "account_id": "0x371b6d542a2471c1e8f38495e0dea578d00a377c",
      "last_timestamp_ms": 1770481446248,
      "last_id": null,
      "last_success_at": "2026-02-07 16:24:06"
    },
    {
      "endpoint": "fills:hyperliquid:0x371b6d542a2471c1e8f38495e0dea578d00a377c",
      "source": "hyperliquid",
      "account_id": "0x371b6d542a2471c1e8f38495e0dea578d00a377c",
      "last_timestamp_ms": 1770452234227,
      "last_id": "201719353085911",
      "last_success_at": "2026-02-07 16:24:05"
    }
  ]
}
```

`/api/account/open-positions` output:

```json
{
  "source": "hyperliquid",
  "account_id": "0x371b6d542a2471c1e8f38495e0dea578d00a377c",
  "account": {
    "total_equity": 360.32653,
    "available_balance": 360.32653,
    "margin_balance": 0,
    "timestamp": "2026-02-07T16:24:06.248999+00:00"
  },
  "open_positions": []
}
```

## Funding Status

Funding ingestion is deferred by default.

- Default behavior: `funding = 0` for Hyperliquid sync.
- Guard flag for future implementation testing:
  - `HYPERLIQUID_ENABLE_FUNDING=true` will currently raise a clear `NotImplementedError`.

## Backfilling price bars (charts + MAE/MFE prerequisites)

The journal stores canonical `1m` bars in SQLite `price_bars` keyed by `(source, symbol, timeframe, timestamp)`.

Backfill trade-window bars:

- ApeX trades:
  - `.venv/bin/python -m trade_journal.backfill_price_bars_apex --db data/trade_journal.sqlite`
- Hyperliquid trades + Hyperliquid BTC benchmark bars spanning the ApeX trade-history window:
  - `.venv/bin/python -m trade_journal.backfill_price_bars_hyperliquid --db data/trade_journal.sqlite`

Both commands are idempotent (upserts) and safe to re-run. Use `--dry-run` to preview windows without fetching data.

To benchmark-backfill a specific test window:

- `.venv/bin/python -m trade_journal.backfill_price_bars_hyperliquid --benchmark-only --benchmark-symbol BTC-USDC --benchmark-start 2026-02-02T00:00:00+00:00 --benchmark-end 2026-02-02T06:00:00+00:00`
