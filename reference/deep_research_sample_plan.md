## Status: Non-binding Reference

# Index

- §1 Executive Summary
- §2 Tradezella Feature Map (MVP vs Later)
- §3 ApeX Omni API Integration Plan (endpoints, auth, sync strategy)
- §4 Canonical Data Model (tables/entities + fields + indexes)
- §5 Trade Reconstruction Algorithm (fills → trades; edge cases)
- §6 Analytics & Metrics Spec (formulas + required fields)
- §7 Architecture Option A (fast monolith)
- §8 Architecture Option B (scalable)
- §9 Frontend Page Inventory + UI notes
- §10 Risks & Mitigations
- §11 Milestone Plan (week-by-week)
- §12 Appendix: Links, citations, and any assumptions

---

## §1 Executive Summary

Start with a browser-based single-user trade journal MVP that:

- Ingests fills/orders/PnL from ApeX Omni perps
- Reconstructs round-trip trades with PnL, fees, and timestamps
- Allows tagging and journaling each trade
- Renders key dashboards: equity curve, PnL table, calendar view

Why:

- Automatic journaling is the wedge.
- Manual notes/tags unlock insight.
- Equity curve & calendar are highest ROI visual feedback.

Use a Python monolith with PostgreSQL and a React (or Next.js) dashboard.

Target MVP in 3 weeks. Keep codebase multi-user ready from day 1.

---

## §2 Tradezella Feature Map (MVP vs Later)

**Core MVP:**

- [x] Auto-import from exchange
- [x] Trade reconstruction
- [x] Manual tags + notes per trade
- [x] Equity curve
- [x] PnL table
- [x] Calendar view (PnL per day)
- [x] Win/loss stats
- [x] Manual trade deletion/editing
- [x] Raw fills/order explorer

**Phase 2:**

- [ ] Symbol breakdowns
- [ ] Day-of-week analysis
- [ ] MAE/MFE stats
- [ ] Best/worst hour
- [ ] Tilt streak detection
- [ ] Drawdown curve
- [ ] Histogram visualizations
- [ ] Setup/tag filters
- [ ] R-multiples
- [ ] Trade detail playback

**Phase 3:**

- [ ] Multi-user accounts
- [ ] Shared setups/tags
- [ ] Social journaling
- [ ] Mobile app
- [ ] Screenshot uploads
- [ ] NLP-based trade reviews

---

## §3 ApeX Omni API Integration Plan (endpoints, auth, sync strategy)

**Auth:**

- HMAC-SHA256
- Requires `API-KEY`, `SIGNATURE`, `TIMESTAMP`

**Endpoints:**

- `/v2/fills` — trade fills (core)
- `/v2/orders` — orders history
- `/v2/positions` — open/closed positions
- `/v2/pnl` — realized/unrealized PnL
- `/v2/account` — balance snapshots
- `/v2/funding` — funding payments
- `/v2/fees` — trading fees (if separate)

**Pagination:**

- Cursor-based or `page` + `limit` (varies by endpoint)
- Use highest granularity (fills > orders > positions)

**Sync Strategy:**

- Poll every 5 min for new fills (or user-triggered import)
- Store latest sync timestamp per endpoint
- Re-fetch last 1 hour on every run to catch late fills

**Reconciliation Plan:**

- Use `fill_id` or `order_id + timestamp` as dedup keys
- Store raw JSON for each synced object
- Idempotent upserts
- Handle partial fills by grouping fills → orders
- Backfill support (re-sync full history on demand)
- Handle clock skew via `server_time` from API
- Allow user-triggered resync for bad data

---

## §4 Canonical Data Model (tables/entities + fields + indexes)

**users**

- id (UUID, pk)
- email (optional)
- created_at

**api_credentials**

- id (UUID, pk)
- user_id (fk)
- name
- key
- secret (encrypted)
- created_at

**fills**

- id (pk, from API)
- user_id (fk)
- order_id
- symbol
- side
- price
- size
- fee
- fee_asset
- timestamp
- raw_json

Indexes: `user_id`, `timestamp DESC`

**orders**

- id (pk)
- user_id
- symbol
- side
- type
- status
- avg_price
- quantity
- filled_qty
- timestamp
- raw_json

**positions**

- id (pk or composite key)
- user_id
- symbol
- entry_price
- exit_price
- size
- pnl
- fees
- opened_at
- closed_at

**trades**

- id (UUID, pk)
- user_id
- symbol
- entry_time
- exit_time
- side
- entry_price
- exit_price
- size
- realized_pnl
- total_fees
- tags (array)
- notes (text)

Indexes: `user_id`, `exit_time DESC`

**funding**

- id
- user_id
- symbol
- amount
- timestamp

---

## §5 Trade Reconstruction Algorithm (fills → trades; edge cases)

**Input:** Stream of fills sorted by timestamp

**Process:**

1. Group fills by `symbol + side` to reconstruct continuous positions
2. Accumulate entry fills → build a “position”
3. When position is closed (net size = 0), finalize trade
4. Compute avg entry, avg exit, fees, realized PnL
5. Store trade record + link to fills

**Edge Cases:**

- **Partial fills:** group by order ID or time window
- **Scaling in/out:** record entry and exit as multiple fill events
- **Reversals:** treat position flip as 2 trades (close + new open)
- **Clock skew:** trust API timestamps; store both client and server time
- **Fees:** allocate across entry/exit fills proportionally
- **Funding:** assign funding to trade if within its open/close window

---

## §6 Analytics & Metrics Spec (formulas + required fields)

| Metric              | Formula                                                     |
| ------------------- | ----------------------------------------------------------- |
| Win Rate            | (Winning Trades) / (Total Trades)                           |
| Avg Win             | AVG(PnL WHERE PnL > 0)                                      |
| Avg Loss            | AVG(PnL WHERE PnL < 0)                                      |
| Profit Factor       | SUM(PnL > 0) / ABS(SUM(PnL < 0))                            |
| Payoff Ratio        | Avg Win / ABS(Avg Loss)                                     |
| Expectancy          | (WinRate _ AvgWin) - (LossRate _ AvgLoss)                   |
| MAE (per trade)     | MAX(adverse move before exit)                               |
| MFE (per trade)     | MAX(favorable move before exit)                             |
| R-Multiple          | Realized PnL / Initial Risk                                 |
| Max Drawdown        | MAX(prev peak - trough in equity curve)                     |
| Equity Curve        | Cumulative sum of realized PnL over time                    |
| Time in Trade       | exit_time - entry_time                                      |
| Best/Worst Hour     | Group PnL by hour(entry_time)                               |
| Day of Week Perf    | Group PnL by weekday(entry_time)                            |
| Tilt Detection      | >3 losing trades + increasing size or shorter holding times |
| Overtrading         | # of trades per day vs baseline avg                         |
| Symbol Breakdown    | Group by `symbol`, compute all metrics above                |
| Setup/Tag Breakdown | Group by tag, compute metrics                               |

**Required Fields:**

- entry_time, exit_time
- entry_price, exit_price
- size
- side
- fees
- realized_pnl
- tag[]
- MAE, MFE (optional capture from price stream or post-hoc analysis)

---

## §7 Architecture Option A (fast monolith)

**Overview:**

- Python FastAPI app
- Postgres DB
- Cron scheduler via Celery or APScheduler
- Single Docker container (or Docker Compose)

**Pipeline:**

1. Cron job hits ApeX API → stores fills/orders
2. Job reconstructs trades from raw fills
3. API serves dashboard data
4. Frontend fetches stats and trade objects

**Components:**

- `ingest.py` — polling job
- `models.py` — SQLAlchemy schema
- `reconstruct.py` — trade builder
- `api.py` — FastAPI routes
- `frontend/` — React or Next.js SPA

**Secrets:**  
Use `.env` + Fernet encryption for API secrets

---

## §8 Architecture Option B (scalable)

**Overview:**

- Ingestion worker (Python)
- API service (FastAPI)
- Frontend (Next.js)
- PostgreSQL DB
- Redis (for jobs/cache)
- Background scheduler (Celery + RabbitMQ or Redis)

**Pipeline:**

- Worker pulls from ApeX every X mins (or on webhook if supported)
- Queue raw fills into processing jobs
- Reconstructor service parses fills into trades
- API service fetches data for frontend
- Caching layer for stats

**Secrets Management:**

- Store encrypted creds using Vault or env + Fernet
- Rotateable API key storage per user

**Logging:**

- Audit logs for import runs
- Trade change history (notes, tags)

**Scalability:**

- Queue-based ingestion
- Horizontally scalable API
- Multi-user out of the box

---

## §9 Frontend Page Inventory + UI notes

**Nav Structure:**

- Dashboard
- Calendar
- Trades
- Tags
- Settings

**Components per Page:**

**Dashboard:**

- Equity Curve Chart
- Stats Summary (win rate, PF, avg win/loss)
- Drawdown Chart
- Symbol/Tag breakdown

**Calendar:**

- PnL per day heatmap
- Click day → trades list

**Trades:**

- Table: symbol, size, PnL, tags, date
- Filter by tag/date/symbol
- Row click → Trade Modal

**Trade Modal:**

- Entry/Exit detail
- Fill breakdown
- Notes (editable)
- Tags (editable)
- Link to raw data

**Tags:**

- List of tags
- Aggregate stats per tag
- Create/Edit/Delete tags

**Settings:**

- API keys
- Re-sync controls
- Export data

**Validation:**

- “Disputed trade” flag if fill grouping is ambiguous
- Manual override/edit support
- Fill viewer → trust raw data

---

## §10 Risks & Mitigations

| Risk                                | Mitigation                                               |
| ----------------------------------- | -------------------------------------------------------- |
| Missing fills or clock skew         | Reconcile with redundant order data, allow user reimport |
| API downtime / rate limit           | Backoff retries, scheduled jobs, manual import trigger   |
| Trade grouping errors               | Surface disputed trades in UI for manual review          |
| Data drift (e.g., ApeX API changes) | Wrap API in versioned module with tests                  |
| Misleading dashboards               | Always allow drill-down into raw fills                   |
| Secret leakage                      | Encrypt API keys at rest, scoped permissions             |
| Sync drift                          | Use hash checksums and reconciliation jobs               |

---

## §11 Milestone Plan (week-by-week)

**Week 1:**

- Set up repo + project scaffold
- PostgreSQL schema + SQLAlchemy models
- Ingestion script for fills + orders
- Save raw JSON + dedup logic
- Manual import trigger

**Week 2:**

- Trade reconstruction engine
- FastAPI backend w/ endpoints
- Equity curve + PnL summary logic
- Start frontend (Next.js or React)
- Basic Dashboard + Trades table

**Week 3:**

- Tagging + notes UI
- Trade detail modal
- Calendar view
- Metrics engine (win rate, PF, etc)
- Resync UI controls

**Week 4 (buffer / polish):**

- Frontend cleanup
- Validation / dispute system
- Export JSON/CSV
- Logging + audit
- Docs + Dockerization

---

## §12 Appendix: Links, citations, and any assumptions

- ApeX Omni API: https://apidocs.apex.exchange/
- Assumed perps only, not spot.
- Assumed user has API key + self-hosting setup.
- Assumed no real-time streaming, polling only.
- Assumed no need for external auth (OAuth, etc).
