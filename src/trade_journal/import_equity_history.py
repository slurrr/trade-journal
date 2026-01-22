from __future__ import annotations

import argparse
import json
from pathlib import Path

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.ingest.apex_equity import load_equity_history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize ApeX account balance history into equity snapshots."
    )
    parser.add_argument(
        "history_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to account balance history JSON.",
    )
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--out", type=Path, default=None, help="Output equity history path.")
    parser.add_argument(
        "--min-value",
        type=float,
        default=0.0,
        help="Skip snapshots with total_value <= min_value (default 0).",
    )
    args = parser.parse_args(argv)

    context = resolve_account_context(args.account)
    history_path = args.history_path or resolve_data_path(
        None, context, "account_balance_history.json"
    )
    if not history_path.exists():
        raise SystemExit(f"History file not found: {history_path}")

    result = load_equity_history(
        history_path,
        source=context.source,
        account_id=context.account_id,
        min_value=args.min_value,
    )

    out_path = args.out or resolve_data_path(None, context, "equity_history.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "data": {
            "equity_history": [
                {
                    "timestamp": snap.timestamp.isoformat(),
                    "total_value": snap.total_value,
                }
                for snap in result.snapshots
            ]
        }
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"equity_snapshots {len(result.snapshots)}")
    print(f"equity_skipped {result.skipped}")
    print(f"equity_out {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
