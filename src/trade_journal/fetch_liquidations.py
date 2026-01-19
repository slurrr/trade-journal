from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv
from trade_journal.ingest.apex_liquidations import LiquidationEvent, extract_liquidations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch ApeX Omni liquidation events via historical PnL.")
    parser.add_argument("--limit", type=int, default=None, help="Number of records per page.")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-based).")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON instead of filtered events.")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--all", action="store_true", help="Fetch all pages until empty.")
    parser.add_argument("--max-pages", type=int, default=200, help="Maximum pages to fetch with --all.")
    parser.add_argument("--out", type=Path, default=Path("data/liquidations.json"), help="Output file.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    if args.base_url:
        env["APEX_BASE_URL"] = args.base_url
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)

    if args.all:
        payload = _fetch_all_pages(client, limit=args.limit, max_pages=args.max_pages)
    else:
        payload = client.fetch_historical_pnl(limit=args.limit, page=args.page)

    if args.raw:
        text = json.dumps(payload, indent=2, sort_keys=True)
    else:
        result = extract_liquidations(payload)
        events = result.events
        text = json.dumps([_event_to_dict(event) for event in events], indent=2, sort_keys=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


def _event_to_dict(event: LiquidationEvent) -> dict[str, Any]:
    return {
        "liquidation_id": event.liquidation_id,
        "symbol": event.symbol,
        "side": event.side,
        "size": event.size,
        "entry_price": event.entry_price,
        "exit_price": event.exit_price,
        "total_pnl": event.total_pnl,
        "fee": event.fee,
        "liquidate_fee": event.liquidate_fee,
        "created_at": event.created_at.isoformat(),
        "exit_type": event.exit_type,
        "is_liquidate": True,
        "raw": event.raw,
    }


def _fetch_all_pages(
    client: ApexApiClient,
    limit: int | None,
    max_pages: int,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    page = 0
    while page < max_pages:
        payload = client.fetch_historical_pnl(limit=limit, page=page)
        error = _payload_error(payload)
        if error:
            raise RuntimeError(f"ApeX error while paging: {error}")
        page_records = extract_liquidations(payload).events
        if not page_records:
            break
        records.extend([event.raw for event in page_records])
        if limit is not None and len(page_records) < limit:
            break
        page += 1
    return {"data": {"historicalPnl": records}}


def _payload_error(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code is None:
        return None
    if str(code) in {"0", "200"}:
        return None
    msg = payload.get("msg", "")
    return f"code={code} msg={msg}"


if __name__ == "__main__":
    raise SystemExit(main())
