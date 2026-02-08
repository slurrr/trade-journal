from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Any, Iterable

from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Fetch ApeX Omni fills and print shape.")
    parser.add_argument("--limit", type=int, default=None, help="Number of fills per page.")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-based).")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON instead of summary.")
    parser.add_argument("--env", type=Path, default=app_config.app.env_path, help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--begin", type=str, default=None, help="Begin datetime (local) or epoch ms.")
    parser.add_argument("--end", type=str, default=None, help="End datetime (local) or epoch ms.")
    parser.add_argument("--all", action="store_true", help="Fetch all pages until empty.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=app_config.sync.max_pages,
        help="Maximum pages to fetch with --all.",
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    env = apply_api_settings(env, app_config, base_url_override=args.base_url)
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)

    begin_ms = _parse_datetime_arg(args.begin) if args.begin else None
    end_ms = _parse_datetime_arg(args.end) if args.end else None
    if args.all:
        records = _fetch_all_pages(
            client,
            limit=args.limit,
            begin_ms=begin_ms,
            end_ms=end_ms,
            max_pages=args.max_pages,
        )
        payload = {"data": {"orders": records}}
    else:
        payload = client.fetch_fills(limit=args.limit, page=args.page, begin_ms=begin_ms, end_ms=end_ms)

    if args.raw:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    fill_records = _extract_fill_records(payload)
    print(_summarize_payload(payload, fill_records))
    return 0


def _extract_fill_records(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        for key in ("data", "fills", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [record for record in value if isinstance(record, dict)]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("fills", "list", "orders"):
                value = data.get(key)
                if isinstance(value, list):
                    return [record for record in value if isinstance(record, dict)]
    return []


def _summarize_payload(payload: Any, fill_records: Iterable[dict[str, Any]]) -> str:
    top_keys = list(payload.keys()) if isinstance(payload, dict) else []
    fills = list(fill_records)
    fill_keys = list(fills[0].keys()) if fills else []
    summary = {
        "top_level_keys": top_keys,
        "fill_count": len(fills),
        "fill_keys": fill_keys,
    }
    return json.dumps(summary, indent=2, sort_keys=True)


def _fetch_all_pages(
    client: ApexApiClient,
    limit: int | None,
    begin_ms: int | None,
    end_ms: int | None,
    max_pages: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 0
    while page < max_pages:
        payload = client.fetch_fills(limit=limit, page=page, begin_ms=begin_ms, end_ms=end_ms)
        error = _payload_error(payload)
        if error:
            raise RuntimeError(f"ApeX error while paging: {error}")
        page_records = list(_extract_fill_records(payload))
        if not page_records:
            break
        records.extend(page_records)
        if limit is not None and len(page_records) < limit:
            break
        page += 1
    return records


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


def _parse_datetime_arg(value: str) -> int:
    text = value.strip()
    if text.isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp() * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
