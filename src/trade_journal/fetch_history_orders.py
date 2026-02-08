from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Fetch ApeX Omni history orders.")
    parser.add_argument("--limit", type=int, default=None, help="Number of orders per page.")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-based).")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Deprecated (raw is default). Use --summary to write a summary file instead.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Write a summary file instead of the full payload.",
    )
    parser.add_argument("--env", type=Path, default=app_config.app.env_path, help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--all", action="store_true", help="Fetch all pages until empty.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=app_config.sync.max_pages,
        help="Maximum pages to fetch with --all.",
    )
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--out", type=Path, default=None, help="Output file.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    env = apply_api_settings(env, app_config, base_url_override=args.base_url)
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)
    context = resolve_account_context(args.account, env=env)

    if args.all:
        payload = _fetch_all_pages(client, limit=args.limit, max_pages=args.max_pages)
    else:
        payload = client.fetch_history_orders(limit=args.limit, page=args.page)

    if args.summary:
        orders = list(_extract_orders(payload))
        summary = {
            "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else [],
            "order_count": len(orders),
            "order_keys": list(orders[0].keys()) if orders else [],
        }
        text = json.dumps(summary, indent=2, sort_keys=True)
    else:
        text = json.dumps(payload, indent=2, sort_keys=True)

    if args.out:
        out_path = args.out
    elif args.summary:
        out_path = resolve_data_path(None, context, "history_orders_summary.json")
    else:
        out_path = resolve_data_path(None, context, "history_orders.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    return 0


def _extract_orders(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        for key in ("data", "orders", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [record for record in value if isinstance(record, dict)]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("orders", "list", "records"):
                value = data.get(key)
                if isinstance(value, list):
                    return [record for record in value if isinstance(record, dict)]
        if isinstance(data, list):
            return [record for record in data if isinstance(record, dict)]
    return []


def _fetch_all_pages(
    client: ApexApiClient,
    limit: int | None,
    max_pages: int,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    page = 0
    while page < max_pages:
        payload = client.fetch_history_orders(limit=limit, page=page)
        error = _payload_error(payload)
        if error:
            raise RuntimeError(f"ApeX error while paging: {error}")
        page_records = list(_extract_orders(payload))
        if not page_records:
            break
        records.extend(page_records)
        if limit is not None and len(page_records) < limit:
            break
        page += 1
    return {"data": {"orders": records}}


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
