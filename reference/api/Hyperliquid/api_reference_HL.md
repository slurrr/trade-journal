# Hyperliquid API Scratchpad (Perps)

This file is a **working scratchpad** for the Hyperliquid integration. It is not meant to be perfect or complete; keep it current as the team learns specifics.

- Target: **mainnet**, **perps only**
- Decisions: create formal decision records in `docs/decisions/` for any non-trivial choices.
- Symbol display convention: `COIN-USDC` (e.g., `BTC-USDC`) while mapping to HL instruments by `coin`.
- Account identity: wallet address (`docs/decisions/0039-hyperliquid-account-id-wallet-address.md`).

## Known endpoints / URLs (seed)

- WebSocket: `wss://api.hyperliquid.xyz/ws`
- REST info endpoint: `POST https://api.hyperliquid.xyz/info`

## REST payload notes (likely shapes; confirm during integration)

- Symbols/constraints: `{"type":"meta"}`
  - Uses `universe[].name` (coin) and `universe[].szDecimals` for size precision.
- Mid prices: `{"type":"allMids"}`
  - Returns coin→price map; used as primary source for `/api/price/{symbol}` reference prefill.
- L2 book: `{"type":"l2Book","coin":"BTC"}`
  - Returns bid/ask ladders (`levels`) used by `/api/market/depth-summary/{symbol}`.
- Candles: `{"type":"candleSnapshot","req":{"coin":"BTC","interval":"15m","startTime":<ms>,"endTime":<ms>}}`
  - Used by `/risk/atr-stop`; currently mapped for `3m`, `15m`, `1h`, `4h` (and other common intervals).
- Account/positions: `{"type":"clearinghouseState","user":"0x..."}` (used for `/api/account/summary` and `/api/positions`).
  - Response includes:
    - `marginSummary`: `{"accountValue": "...", "totalMarginUsed": "...", "totalNtlPos": "...", "totalRawUsd": "..."}`
    - `crossMarginSummary`: similar fields
    - `withdrawable`: `"..."` (string)
    - `assetPositions`: list of `{"type":"perp","position": {...}}`
      - `position.coin`: `"BTC"`
      - `position.szi`: signed size (string/number); sign implies long/short
      - `position.entryPx`, `position.liquidationPx`
      - `position.positionValue`, `position.marginUsed`
      - `position.unrealizedPnl`, `position.returnOnEquity`
      - `position.leverage`, `position.cumFunding`, `position.maxLeverage`
- Open orders: `{"type":"openOrders","user":"0x..."}` (used for `/api/orders`).
- User fills (v1 journal ground truth): `{"type":"userFillsByTime","user":"0x...","startTime":<ms>,"endTime":<ms>,"aggregateByTime":false}`
  - Returns a list of fills (cap documented as 2000 per response; only 10,000 most recent fills are accessible).
  - Fill fields (example keys):
    - `tid`: stable execution id (int) → use as `fill_id`
    - `time`: execution timestamp (ms)
    - `coin`: e.g. `"BTC"` → display symbol `BTC-USDC`
    - `side`: `"B"` (buy) / `"A"` (ask/sell)
    - `px`, `sz`: price/size strings
    - `fee`, `feeToken`: fee amount and token
    - optional: `builderFee`
    - `oid`: order id (optional but useful for linking)
    - `dir`: human string like `"Close Long"` / `"Open Short"` (nice for debugging)

## Signed exchange actions (possible; confirm during integration)

- Endpoint: `POST /exchange`
- Payload shape:
  - `action`: HL action object (`order`, `cancel`, ...)
  - `nonce`: ms timestamp nonce
  - `signature`: EIP-712 signature (`r`, `s`, `v`)
  - `vaultAddress`: currently `null`
- Candidate actions we may need (journal sync is read-only, but these matter for future automation tooling):
  - place/cancel orders
  - close positions (reduce-only)
  - manage TP/SL trigger orders

## WebSocket quick notes

### Trade prints (public)

- WS: `wss://api.hyperliquid.xyz/ws`
- Subscribe: `{"method":"subscribe","subscription":{"type":"trades","coin":"BTC"}}`

### Potential stream usage (confirm during integration)

- `allMids`: reference/mid prices for quick checks and/or price-series prefill.
- `orderUpdates`: order lifecycle updates for a future live trading view (not required for the journal MVP).
- `userEvents`: account-level events that may help trigger snapshot refreshes.

### TODO (fill in during Phase 0)

- User/account stream subscription(s) (auth requirements?)
- Price stream(s): mids / mark / oracle / etc.
- Orderbook stream(s) (L2 book) and snapshot behavior
- Fill/order update stream(s)
- Reconnect + resubscribe rules

## REST quick notes (TODO)

Capture the specific request/response shapes needed by this app:

- Meta / universe (symbols + constraints)
- Reference price (for `GET /api/price/{symbol}`): mid / mark / last trade (confirm best HL source)
- Candle history (supports UI timeframes `3m`, `15m`, `1h`, `4h`)
- L2 orderbook snapshot (for depth summary)
- Account summary (equity/margin/uPNL)
- Open positions
- Open orders
- Place order
- Cancel order
- Place TP/SL trigger orders and cancel them

## Authentication (Agent Wallet) — TODO

Hyperliquid supports an “API wallet / agent wallet” concept (agent private key) for signing requests.

Items to confirm and document here:
- Signing algorithm and payload format
- Nonce/time requirements
- How agent keys map to a master account / subaccount
- WS auth requirements (if any) vs REST-only auth
- Operational rotation plan (revoke/replace agent)

The final chosen approach must be captured as a decision record (see `docs/decisions/`).
