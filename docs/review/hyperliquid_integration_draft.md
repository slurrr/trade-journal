# Hyperliquid Venue Integration (Draft)

Date: 2026-02-06

## Context

This repo is a single-user, local-first trade journal. Ground truth comes from the venue’s source data (see `AGENTS.md`) and we currently ingest ApeX Omni, reconstruct trades from fills, and persist normalized records to SQLite.

The next venue is **Hyperliquid (perps)**. Core constraints to preserve:

- Fills-first ingestion and explainable reconstruction (Decision 0003, 0001, 0004).
- All timestamps normalized to UTC (Decision 0006).
- Idempotent incremental sync with overlap windows and per-endpoint checkpoints (Decision 0020, 0022).
- Multi-account support via `accounts` table + `source`/`account_id` scoping (Decision 0031, 0041).
- Hyperliquid account identity is the wallet address (Decision 0039).
- Funding ingestion is required but deferred for v1 until validated (Decision 0040).

## Goals (v1)

- Add Hyperliquid as a new supported `source` in `config/accounts.toml` (e.g. `[accounts.hl_main] source="hyperliquid"`).
- Ingest Hyperliquid **fills** into the existing `fills` table and reconstruct trades using the existing pipeline.
- Persist an **account snapshot** that includes open positions (at least in `account_snapshots.raw_json`) so the UI can show open positions quickly.
- Keep the sync contract consistent: safe re-runs, overlap window dedupe, no silent gaps.

## Non-goals (v1)

- Trading / order placement / cancelation / TP/SL management.
- Perfect parity with ApeX “orders-based” R/stop/target metrics (these are optional and venue-dependent).
- Funding fees (tracked as a known gap; Decision 0040).

## Account and Identity

### Config shape

- The account “name” is the TOML key: `[accounts.hl_main]`.
- `account_id` is the HL wallet address (e.g. `0x...`) and is canonicalized to lowercase (Decision 0039).
  - Canonicalization must happen in the resolution path (e.g. `resolve_account_context`) so all ingest/sync/UI code sees the same normalized identifier.

Example:

```toml
[accounts.hl_main]
source = "hyperliquid"
exchange = "hyperliquid"
account_id = "0xabc123..."
data_dir = "data/hl_main"
base_currency = "USDC"
starting_equity = ""
active = true
```

### Account identity collisions (multi-venue)

Trades and ingested records are scoped by `(source, account_id)`, so account metadata must be as well (Decision 0041).

Define a canonical `account_key` string for UI filtering and internal joins:

```
account_key = f"{source}:{account_id}"
```

### SQLite `accounts` row

On sync, upsert one `accounts` row per configured account. With Decision 0041, `accounts` is keyed by `(source, account_id)` rather than `account_id` alone.

Fields:

- `source`: `"hyperliquid"`
- `account_id`: lowercased wallet address
- `name`: `hl_main`
- `exchange`: `"hyperliquid"` (or `exchange` override)
- `base_currency`: `"USDC"` (recommended default)
- `raw_json`: include `{ "wallet": "0x...", "data_dir": "..." }`

## Data sources (Hyperliquid)

Hyperliquid’s documented entry point in this repo is `reference/api/Hyperliquid/api_reference_HL.md`.

We expect to use the REST info endpoint:

- `POST https://api.hyperliquid.xyz/info`

and query types including:

- Universe/meta: `{"type":"meta"}`
- Account/positions snapshot: `{"type":"clearinghouseState","user":"0x..."}`
- Open orders (optional for future R/target work): `{"type":"openOrders","user":"0x..."}`
- Candles (future price-series/excursions): `{"type":"candleSnapshot","req":{...}}`
- **User fills** / execution history (locked contract for v1):
  - Request type: `userFillsByTime`
  - Request payload:
    - `{"type":"userFillsByTime","user":"0x...","startTime":<ms>,"endTime":<ms>,"aggregateByTime":false}`
  - Response: list of fill objects; the stable execution id is `tid` and the execution timestamp is `time` (ms).
  - Limits/availability: responses are capped (documented cap is 2000 fills per call), and only the most-recent fills are available (documented cap is 10,000 fills).

## Normalized storage mapping

### Fills (required)

We will normalize each HL execution into the existing `Fill` model (`src/trade_journal/models.py`):

- `fill_id`: `tid` (stable execution id), stringified. If missing, fallback to a deterministic content hash (Decision 0020 fallback).
- `order_id`: `oid` (order id) if present (optional).
- `symbol`: display `COIN-USDC` (e.g. `BTC-USDC`), derived from `coin`. Preserve `coin` in `raw_json`.
- `side`: normalized to `"BUY"` / `"SELL"`.
  - Map `side == "B"` → `"BUY"`, `side == "A"` → `"SELL"` (ask).
- `price`, `size`: floats, with correct precision handling (store raw values in `raw_json`).
- `fee`: numeric, parsed from `fee` if present; default `0.0`.
- `fee_asset`: parsed from `feeToken` if present; else null.
- `timestamp`: UTC-aware datetime.
- `source`: `"hyperliquid"`
- `account_id`: lowercased wallet address
- `raw_json`: full raw record

Trade reconstruction remains unchanged (one-way netting, flat-to-flat) and will naturally scope by `(source, account_id, symbol)`.

### Account snapshot / open positions (required for fast UI)

We will continue using `account_snapshots`:

- Columns: `total_equity`, `available_balance`, `margin_balance` if HL provides fields that map cleanly.
- `raw_json`: store the entire `clearinghouseState` payload (or a minimally transformed version) including open positions.

#### Minimal normalized open-positions view contract

Do not parse venue-specific JSON directly in templates. Instead, normalize snapshot payloads into a minimal venue-neutral view model.

Define `open_positions` as:

```python
open_positions: list[dict[str, object]] = [
  {
    "account_key": "hyperliquid:0x...",
    "symbol": "BTC-USDC",
    "side": "LONG" | "SHORT",
    "size": float,
    "entry_price": float | None,
    "position_value": float | None,
    "unrealized_pnl": float | None,
    "leverage": float | None,
    "margin_used": float | None,
    "liquidation_price": float | None,
    "return_on_equity": float | None,
  },
]
```

For Hyperliquid, this list is derived from `clearinghouseState.assetPositions[].position` fields (e.g. `coin`, `szi`, `entryPx`, `positionValue`, `unrealizedPnl`, `leverage`, `marginUsed`, `liquidationPx`, `returnOnEquity`).

Templates render `open_positions` only; `account_snapshots.raw_json` remains for audit/debug.

### Orders (optional, later)

HL openOrders may be persisted to the existing `orders` table if we decide it is useful for:

- stop/target inference
- order intent analytics

This is explicitly optional; v1 can omit orders to keep scope tight.

### Funding (required, deferred)

Funding fees will be implemented after v1 once HL funding payment data is validated (Decision 0040).

## Sync design (REST-first)

### Guiding principle

Re-use the existing ApeX sync contract patterns:

- Per-endpoint checkpoint keys in `sync_state` including `source` and `account_id`.
- Overlap window re-fetch and dedupe via upsert keys (Decision 0020).
- Checkpoints only advance on successful completion (Decision 0022).

### Proposed endpoints to checkpoint

For HL we want:

- `fills` (time-filtered)
- `account_snapshot` (point-in-time, no paging; can store timestamp and always upsert)
- `open_orders` (optional; point-in-time)

Checkpoint behavior:

- **Fills:**
  - Use `userFillsByTime` with `startTime`/`endTime` in epoch milliseconds.
  - Paging rule: request a time range; if the response hits the cap, advance the range cursor using the newest returned `time`.
  - To avoid missing same-millisecond fills, do not require strict `time` advancement; rely on upsert dedupe by `tid` and stop if the cursor fails to advance.
  - Store `last_timestamp_ms` = max(`time`) observed on successful sync. Optionally store `last_id` = max(`tid`) within that timestamp (for debugging/forensics).
  - On resume, query from `(last_timestamp_ms - overlap_ms)` and rely on upserts to dedupe.
- **Account snapshot:** store `last_success_at` only; fetch every sync run.
  - Note: a “light refresh” (snapshot-only) vs “full ingest” split is a useful follow-up, but v1 should land safely with one full sync cycle that includes both fills + snapshot (see Follow-ups).

## UI changes (minimal)

### Dashboard

- Add an “Open Positions” panel driven by the normalized `open_positions` view model (not raw JSON).
- Ensure the copy is venue-neutral (remove “ApeX” phrasing where it is user-facing).

### Account selection

Analytics supports multi-account filtering in DB mode; with Decision 0041 it should filter by `account_key = "{source}:{account_id}"` to avoid cross-venue collisions. For non-DB mode, we remain single-selected-account by `TRADE_JOURNAL_ACCOUNT_NAME`.

For HL v1, we should focus on DB-backed mode (syncing into SQLite) to get multi-account behavior “for free”.

## Engineering tasks (implementation checklist)

0) **Lock the fills contract before coding**
   - Confirm (via a probe script + saved fixture) the exact `userFillsByTime` request/response shape:
     - request keys: `type`, `user`, `startTime`, `endTime`
       - optional: `aggregateByTime` (set `false` for per-fill ingestion)
     - response keys: `tid`, `time`, `coin`, `side`, `px`, `sz`, optional: `fee`, `feeToken`, `oid`, `dir`
   - Confirm the response cap behavior (documented as 2000) and validate a paging strategy that cannot skip or loop indefinitely.

1) **HL REST client**
   - Add `trade_journal/ingest/hyperliquid_api.py` with a small `HyperliquidInfoClient` for `/info`.
   - Add bounded retries + backoff consistent with Decision 0022.

2) **HL payload normalizers**
   - `load_hyperliquid_fills_payload(...) -> list[Fill]`
   - `load_hyperliquid_clearinghouse_state_payload(...) -> account_snapshot dict`
   - Keep raw payloads in `raw_json`.

3) **Schema: source-scoped accounts**
   - Implement Decision 0041 by migrating the `accounts` table to be keyed by `(source, account_id)`.
   - Update any account maps/joins (equity fallback, filters) to use `account_key = "{source}:{account_id}"`.

4) **Sync implementation (central dispatch in `sync_api`)**
   - Add a dispatcher inside `trade_journal/sync_api.py` that routes sync behavior by `context.source`.
   - Keep web auto-sync calling `sync_api.sync_once(...)` unchanged; dispatch happens within `sync_api`.
   - Write to existing tables via `sqlite_store` upserts.
   - Use `sync_state` keys like `fills:hyperliquid:<wallet>` and `account_snapshot:hyperliquid:<wallet>`.

5) **UI: open positions**
   - Parse and render positions from `account_snapshot`.
   - Render from the normalized `open_positions` view model (not raw JSON).

6) **Documentation**
   - Update `config/accounts.toml.example` to show an HL account example.
   - Add any HL env var placeholders to `.env.example` only if required.

## Testing / validation

- Add fixture payloads under `tests/` (or `data/` if we keep tests minimal) for:
  - HL fills payload → normalized fills count/fields → deterministic IDs stable across re-runs.
  - HL clearinghouseState payload → snapshot persisted → positions visible to UI parser.
- Run `trade_journal/verify.py`-style sanity checks for HL:
  - trade reconstruction yields “flat-to-flat” with no persistent open-position drift for a known dataset
  - timestamps are UTC-aware and ordered deterministically (Decision 0006)

## Known gaps / follow-ups

- **Funding ingestion** (required): add the correct HL funding payment endpoint mapping and attribution to trades (Decision 0007 style), then remove the v1 limitation noted in Decision 0040.
- Split “sync” into snapshot-only refresh vs full ingest if we need a more responsive UI without WebSockets.
- Add frontend polling to refresh Open Positions without page reload (Decision 0042).
- Price-series/excursions for HL: implement a HL candle client compatible with `PriceBar` so MAE/MFE/ETD can be computed for HL trades.
- Orders/R metrics for HL: only if HL provides reliable TP/SL/stop intent via REST and we want parity.

## Open questions (to answer during implementation)

- Fee fields: are fees present per execution, and are they always in USDC for perps?
- Snapshot fields: how to map HL balances/equity fields into `total_equity`, `available_balance`, `margin_balance` consistently across time?
