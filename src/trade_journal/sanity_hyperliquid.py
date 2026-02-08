from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trade_journal.config.accounts import resolve_account_context
from trade_journal.ingest.apex_api import load_dotenv
from trade_journal.ingest.hyperliquid_api import HyperliquidInfoClient, HyperliquidInfoConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Hyperliquid account sanity check.")
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument("--lookback-hours", type=float, default=24.0, help="Window for recent fill probe.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    context = resolve_account_context(args.account, env=env)
    if context.source != "hyperliquid":
        raise ValueError(f"Account '{context.name}' is source={context.source}, expected hyperliquid.")

    client = HyperliquidInfoClient(HyperliquidInfoConfig.from_env(env))
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=max(0.0, float(args.lookback_hours)))

    snapshot = client.fetch_clearinghouse_state(context.account_id or "")
    fills = client.fetch_user_fills_by_time(
        user=context.account_id or "",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(now.timestamp() * 1000),
        aggregate_by_time=False,
    )

    output = _build_output(context.name, context.account_id or "", snapshot, fills, start, now)
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        _print_human(output)
    return 0


def _build_output(
    account_name: str,
    account_id: str,
    snapshot: Mapping[str, Any],
    fills: Sequence[Mapping[str, Any]],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    equity = _first_float(snapshot, "accountValue")
    margin = snapshot.get("marginSummary")
    if equity is None and isinstance(margin, dict):
        equity = _first_float(margin, "accountValue")

    times = [
        value
        for value in (_timestamp_ms(item.get("time")) for item in fills if item.get("time") is not None)
        if value is not None
    ]
    times.sort()
    first_fill = _to_iso(times[0]) if times else None
    last_fill = _to_iso(times[-1]) if times else None

    return {
        "account_name": account_name,
        "account_id": account_id,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "snapshot_equity": equity,
        "recent_fill_count": len(fills),
        "recent_fill_first_time": first_fill,
        "recent_fill_last_time": last_fill,
    }


def _print_human(output: dict[str, Any]) -> None:
    print(f"account_name {output['account_name']}")
    print(f"account_id {output['account_id']}")
    print(f"window_start {output['window_start']}")
    print(f"window_end {output['window_end']}")
    print(f"snapshot_equity {output['snapshot_equity']}")
    print(f"recent_fill_count {output['recent_fill_count']}")
    print(f"recent_fill_first_time {output['recent_fill_first_time']}")
    print(f"recent_fill_last_time {output['recent_fill_last_time']}")


def _timestamp_ms(value: Any) -> int | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1e12:
        return int(numeric)
    return int(numeric * 1000)


def _to_iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000.0, tz=timezone.utc).isoformat()


def _first_float(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


if __name__ == "__main__":
    raise SystemExit(main())
