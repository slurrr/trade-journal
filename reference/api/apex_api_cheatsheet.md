# ApeX Omni API Cheat Sheet (Journal Ingestion)

Scope: REST read-only endpoints + signing details required for this trade journal.
This is a trimmed version tailored to ingestion and reconstruction only.

## Base URLs & Versions

REST base endpoints (the client will append `/api` if missing):

- Testnet: `https://testnet.omni.apex.exchange/api`
- Mainnet: `https://omni.apex.exchange/api`

All endpoints are under `/v3/...`.

## Authentication & Signing

Required headers:

- `APEX-API-KEY`
- `APEX-PASSPHRASE`
- `APEX-TIMESTAMP`
- `APEX-SIGNATURE`

Signature payload:

```
message = timestamp + method + request_path + dataString
```

- `timestamp` is unix ms.
- `method` is uppercase (GET/POST).
- `request_path` is the full path including `/api` if your base URL includes it (e.g., `/api/v3/fills`).
- `dataString` is a sorted `key=value&...` string from params for POSTs.
- For GETs, the signer appends the query to the path and uses an empty `dataString`.
- The SDK signer uses `base64(secret)` as the HMAC key (not decode).
- HMAC-SHA256 output is base64 encoded for `APEX-SIGNATURE`.

If you see `code=20016` (Failed to check signature), verify the base URL path
and the signing string above.

## Endpoints Used Here (Read-only)

- `GET /v3/fills` — fill history (primary source for reconstruction).
- `GET /v3/historical-pnl` — closed position PnL (used for reconciliation).
- `GET /v3/funding` — funding fees (optional, for net PnL).
- `GET /v3/account` — open positions (optional, to detect open trades).

## Data Conventions

- REST uses camelCase fields.
- Symbols usually `"BTC-USDT"`.
- Timestamps are unix ms.
