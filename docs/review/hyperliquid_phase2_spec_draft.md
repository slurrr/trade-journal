# Hyperliquid Phase 2 – Parity + Benchmark Continuity (Draft)

Phase 1 established Hyperliquid as a supported `source` with:

- automated DB sync of fills + account snapshots (+ basic order ingestion)
- venue-scoped OHLC storage in `price_bars`
- trade-detail charts + MAE/MFE computed from venue-scoped bars when available

Phase 2 focuses on closing the remaining parity gaps vs ApeX and hardening ongoing operation.

## Goals

- Achieve functional parity with ApeX where the venue exposes equivalent source data:
  - funding ingestion (so funding is visible and attributable)
  - equity history curve (account value over time)
  - liquidation/forced-close visibility (if available from Hyperliquid source data)
- Keep benchmark bars continuously populated going forward (even if long-history backfill remains deferred).
- Reduce operational “silent failure” modes (caps, 429s, missing endpoints).

### Metric semantics (Phase 2 requirement)

Use **gross PnL** as the default basis for aggregate performance metrics and charts.

- Funding should be ingested and displayed, and can be used for “net PnL” views, but the default KPI set should not silently switch to net.
- Any ratio metrics that mix PnL with excursions (e.g. MFE capture) must explicitly choose gross vs net and remain consistent across venues.

## Non-Goals

- Multi-user support, auth, cloud, or any SaaS features.
- Perfect historical backfill for Hyperliquid 1m benchmark bars (see Decision 0043 note).
- Replacing fill-based trade reconstruction as the ground truth.

## Current Gaps vs ApeX (as of Phase 1)

Hyperliquid currently syncs:

- `fills`
- `orders` (best effort via `historicalOrders`, `openOrders`, and `orderStatus` for recent fill oids)
- `account_snapshots`

Hyperliquid does **not** yet provide:

- `funding` ingestion (Decision 0040)
- `account_equity` history (ApeX has `/v3/history-value`)
- `historical_pnl` reconciliation + liquidation extraction (ApeX uses `/v3/historical-pnl` and derives liquidations)

## Phase 2 Work Items

### 1) Funding ingestion (Hyperliquid → `funding` table)

Objective: make `Trade.realized_pnl_net` correct for Hyperliquid by applying funding events (same flow as ApeX).

Authoritative source contract (lock for implementation):

- Endpoint: `POST https://api.hyperliquid.xyz/info`
- Request shape: `{"type":"userFunding","user":"0x...","startTime":<ms>,"endTime":<ms>}`
- Response shape: list of funding deltas with keys like:
  - `time` (ms)
  - `hash` (string; treat as stable id)
  - `delta` object with `type="funding"`, `coin`, `usdc`, `szi`, `fundingRate`
- Pagination/limits: treat as capped per response. Page by advancing `startTime` to `last_time_ms + 1` until fewer than the cap are returned or `startTime >= endTime`.
- Idempotency (avoid collisions): use a composite id:
  - `funding_id = f"hyperliquid:{wallet}:{hash}:{time_ms}:{coin}"`
  - If `hash` is missing, use `funding_id = f"hyperliquid:{wallet}:{time_ms}:{coin}:{usdc}"`
- Normalization mapping into `FundingEvent`:
  - `symbol = f"{coin}-USDC"`
  - `side = "BUY"` if `szi > 0` else `"SELL"` (direction of held position)
  - `rate = fundingRate`
  - `position_size = abs(szi)`
  - `funding_value = usdc`
  - `funding_time = time`
  - `price`: if HL does not provide mark/index price, derive best-effort as `abs(usdc) / (abs(szi) * abs(fundingRate))` when all terms are non-zero; otherwise store `0.0` and rely on `raw_json` for provenance.

Deliverables:

- Implement `load_hyperliquid_funding_payload(...) -> list[FundingEvent]` (similar to ApeX normalizer).
- Implement `_sync_hyperliquid_funding(...)` in `sync_api` and remove the `NotImplementedError` scaffold behind `HYPERLIQUID_ENABLE_FUNDING`.

Acceptance:

- Running `trade_journal.sync_api --account hl_main` populates `funding` rows for Hyperliquid.
- `compute_excursions`/web analytics show non-zero `total_funding` when present.

### 2) Equity history (Hyperliquid → `account_equity` table)

Objective: have an equity curve sourced from Hyperliquid account value over time (not just PnL-derived curves).

Approach:

- On each Hyperliquid sync cycle, write a point into `account_equity` using `clearinghouseState.marginSummary.accountValue` and the snapshot timestamp.
- Cadence rule (deterministic default):
  - insert at most once per 60 seconds per `(source, account_id)`
  - and only if `accountValue` changed by at least `0.01` (USD) since the last stored point (or no previous point exists).
  - The 60s check uses the snapshot timestamp from `clearinghouseState` (not wall-clock insert time).

Acceptance:

- `account_equity` has rows for `source="hyperliquid"` with the cadence rule above.
- UI can display an equity history chart for Hyperliquid accounts (or reuse existing equity curve rendering if compatible).

### 3) Liquidation / forced-close visibility (Hyperliquid)

Objective: approximate feature parity with ApeX liquidation events.

Notes:

- If Hyperliquid exposes liquidation events in `/info`, ingest them into the existing `liquidations` table.
- If not exposed, define an explicit “unsupported” status and avoid speculative inference from fills (unless later accepted as a decision).

Acceptance:

- Either:
  - liquidation events appear for HL and are displayed similarly to ApeX, or
  - UI clearly indicates liquidations are unavailable for HL (no silent gaps).

### 4) Benchmark continuity (Hyperliquid BTC-USDC → `price_bars`)

Objective: keep BTC benchmark bars present going forward without manual backfills.

Constraints:

- Hyperliquid `candleSnapshot` appears capped (recent-history only). Phase 2 does **not** attempt full historical backfill.

Approach:

- Process boundary (explicit requirement):
  - implement benchmark maintenance as a **post-sync step inside `sync_api`** so it runs for both:
    - the web auto-sync loop, and
    - CLI/scheduler usage (`trade_journal.sync_api` invoked via cron/systemd)
  - document an external scheduler option (daily) as supported ops guidance.
- Data ingestion:
  - Maintain `BTC-USDC` benchmark bars in `price_bars` via `candleSnapshot`:
    - Primary: `timeframe="1m"` by fetching a trailing window of `N=5000` bars (compute window as `end=now`, `start=now-(N minutes)`).
    - Fallback: `timeframe="5m"` by fetching a trailing window of `N=5000` bars (~17.3 days).
  - Implementation detail: the maintenance step must not depend on a CLI convenience flag; it can call a shared helper that computes `(start_ms,end_ms)` from `now` and `N`.
- Coverage rule (deterministic):
  - Define `window_start_utc` and `window_end_utc` (requested time bounds).
  - For `timeframe="1m"`, align to minute boundaries:
    - `aligned_start = floor_to_minute(window_start_utc)`
    - `aligned_end = floor_to_minute(window_end_utc)`
  - Effective benchmark window is defined on bar-start timestamps as: `[aligned_start, aligned_end]`.
  - Series is “fully covered” if:
    - earliest bar timestamp `<= aligned_start`
    - latest bar timestamp `>= aligned_end`
    - and there are no gaps between consecutive bars greater than `2 * timeframe` within `[aligned_start, aligned_end]`.
  - Benchmark return selection rule (UI/analytics): prefer `1m` if fully covered else fall back to `5m` if fully covered else `None` + explicit “benchmark unavailable” note.

Acceptance:

- After several days of running the journal, `price_bars` contains BTC-USDC 1m bars continuously across that run window.
- After being offline for up to the chosen coarse-series span (e.g. ~17 days for 5m), benchmark returns are still available (with coarser granularity) without requiring external providers.

### 5) Caps + rate limiting hardening

Objective: fewer “looks synced but isn’t” situations.

Items:

- Make 429 behavior visible and actionable:
  - log a clear “throttled” warning, suggest increasing `--sleep-seconds` / env retry values
- Make Hyperliquid fill-history caps explicit in the UI/operator docs:
  - current implementation warns when `userFillsByTime` likely hits the recent-history cap
- Add a “resume/backfill horizon” debug view:
  - show oldest available fill timestamp per source/account

Acceptance (observability):

- `GET /api/sync-state` (or a dedicated diagnostics endpoint) includes, per `(endpoint, source, account_id)`:
  - `throttled_count` (429s encountered since last success)
  - `cap_detected` (boolean for known “recent history cap” conditions)
  - `oldest_fill_ts` (best-effort oldest timestamp observed/available for that account)

## Operator Notes (Phase 2)

- Benchmark history before the journal was running is deferred; treat benchmark comparisons as “from first stored benchmark bar onward”.
- For a one-off benchmark fill: use a bounded window (`--benchmark-start/--benchmark-end`) or a trailing window helper (if available in tooling).

## Open Questions to Resolve Before Coding

- Do we have a reliable liquidation/forced-close endpoint on Hyperliquid?
