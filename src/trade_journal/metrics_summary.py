from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.metrics.summary import AggregateMetrics, compute_aggregate_metrics
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute aggregate trade metrics.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=Path("data/fills.json"),
        help="Path to ApeX fills export (json/csv/tsv).",
    )
    parser.add_argument(
        "--funding",
        type=Path,
        default=None,
        help="Optional funding export (json/csv/tsv) to apply to trades.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--out", type=Path, default=None, help="Write output to a file instead of stdout.")
    args = parser.parse_args(argv)

    result = load_fills(args.fills_path)
    trades = reconstruct_trades(result.fills)

    if result.skipped:
        print(f"Skipped {result.skipped} fill rows during normalization.", file=sys.stderr)

    funding_path = args.funding
    if funding_path is None:
        default_funding = Path("data/funding.json")
        if default_funding.exists():
            funding_path = default_funding

    if funding_path is not None:
        funding_result = load_funding(funding_path)
        attributions = apply_funding_events(trades, funding_result.events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        if funding_result.skipped:
            print(
                f"Skipped {funding_result.skipped} funding rows during normalization.",
                file=sys.stderr,
            )
        if unmatched:
            print(f"Unmatched funding events: {unmatched}.", file=sys.stderr)

    metrics = compute_aggregate_metrics(trades)

    out_path = args.out
    if out_path is None:
        out_path = Path("data/metrics.json")

    if args.json or out_path.suffix.lower() == ".json":
        payload = _metrics_to_dict(metrics)
        text = json.dumps(payload, indent=2, sort_keys=True)
    else:
        text = _format_metrics(metrics)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")

    return 0


def _metrics_to_dict(metrics: AggregateMetrics) -> dict[str, float | int | None]:
    return {
        "total_trades": metrics.total_trades,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "breakevens": metrics.breakevens,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "expectancy": metrics.expectancy,
        "avg_win": metrics.avg_win,
        "avg_loss": metrics.avg_loss,
        "largest_win": metrics.largest_win,
        "largest_loss": metrics.largest_loss,
        "max_consecutive_wins": metrics.max_consecutive_wins,
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "total_gross_pnl": metrics.total_gross_pnl,
        "total_net_pnl": metrics.total_net_pnl,
        "total_fees": metrics.total_fees,
        "total_funding": metrics.total_funding,
        "avg_duration_seconds": metrics.avg_duration_seconds,
        "total_duration_seconds": metrics.total_duration_seconds,
    }


def _format_metrics(metrics: AggregateMetrics) -> str:
    lines = [
        f"total_trades {metrics.total_trades}",
        f"wins {metrics.wins}",
        f"losses {metrics.losses}",
        f"breakevens {metrics.breakevens}",
        f"win_rate {_format_float(metrics.win_rate)}",
        f"profit_factor {_format_float(metrics.profit_factor)}",
        f"expectancy {_format_float(metrics.expectancy)}",
        f"avg_win {_format_float(metrics.avg_win)}",
        f"avg_loss {_format_float(metrics.avg_loss)}",
        f"largest_win {_format_float(metrics.largest_win)}",
        f"largest_loss {_format_float(metrics.largest_loss)}",
        f"max_consecutive_wins {metrics.max_consecutive_wins}",
        f"max_consecutive_losses {metrics.max_consecutive_losses}",
        f"total_gross_pnl {_format_float(metrics.total_gross_pnl)}",
        f"total_net_pnl {_format_float(metrics.total_net_pnl)}",
        f"total_fees {_format_float(metrics.total_fees)}",
        f"total_funding {_format_float(metrics.total_funding)}",
        f"avg_duration_seconds {_format_float(metrics.avg_duration_seconds)}",
        f"total_duration_seconds {_format_float(metrics.total_duration_seconds)}",
    ]
    return "\n".join(lines)


def _format_float(value: float | None) -> str:
    return "na" if value is None else f"{value:.6g}"


if __name__ == "__main__":
    raise SystemExit(main())
