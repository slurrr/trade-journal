from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable

from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Fetch ApeX Omni historical PnL records.")
    parser.add_argument("--limit", type=int, default=None, help="Number of records per page.")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-based).")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON instead of summary.")
    parser.add_argument("--env", type=Path, default=app_config.app.env_path, help="Path to .env file.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    env = apply_api_settings(env, app_config)
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)

    payload = client.fetch_historical_pnl(limit=args.limit, page=args.page)

    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    records = _extract_records(payload)
    print(_summarize_payload(payload, records))
    return 0


def _extract_records(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [record for record in value if isinstance(record, dict)]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("list", "records", "pnl", "history", "historicalPnl"):
                value = data.get(key)
                if isinstance(value, list):
                    return [record for record in value if isinstance(record, dict)]
    return []


def _summarize_payload(payload: Any, records: Iterable[dict[str, Any]]) -> str:
    top_keys = list(payload.keys()) if isinstance(payload, dict) else []
    items = list(records)
    record_keys = list(items[0].keys()) if items else []
    summary = {
        "top_level_keys": top_keys,
        "record_count": len(items),
        "record_keys": record_keys,
    }
    return json.dumps(summary, indent=2, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
