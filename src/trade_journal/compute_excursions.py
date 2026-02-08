from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path

from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.config.app_config import load_app_config
from trade_journal.ingest.apex_api import load_dotenv
from trade_journal.ingest.apex_funding import load_funding
from trade_journal.ingest.apex_omni import load_fills
from trade_journal.ingest.hyperliquid_api import HyperliquidInfoConfig
from trade_journal.metrics.excursions import apply_trade_excursions
from trade_journal.metrics.excursions import PriceBar
from trade_journal.metrics.series import compute_trade_series, downsample_series
from trade_journal.pricing.hyperliquid_prices import HyperliquidPriceClient
from trade_journal.pricing.apex_prices import ApexPriceClient, PriceSeriesConfig
from trade_journal.reconstruct.funding import apply_funding_events
from trade_journal.reconstruct.trades import reconstruct_trades
from trade_journal.storage import sqlite_reader
from trade_journal.storage.sqlite_store import connect as sqlite_connect
from trade_journal.storage.sqlite_store import init_db, upsert_price_bars


_CANONICAL_TIMEFRAME = "1m"
_CANONICAL_INTERVAL = timedelta(minutes=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute MAE/MFE/ETD and cache to JSON.")
    parser.add_argument(
        "fills_path",
        type=Path,
        nargs="?",
        default=None,
        help="Path to ApeX fills export (json/csv/tsv).",
    )
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument(
        "--funding",
        type=Path,
        default=None,
        help="Optional funding export (json/csv/tsv) to apply to trades.",
    )
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument("--out", type=Path, default=None, help="Output file.")
    parser.add_argument("--series-out", type=Path, default=None, help="Optional trade series cache output.")
    parser.add_argument(
        "--series-max-points",
        type=int,
        default=None,
        help="Max points per trade series (downsample if needed).",
    )
    parser.add_argument("--local", action="store_true", help="Use local timestamps for keys (default uses UTC).")
    parser.add_argument("--db", type=Path, default=None, help="SQLite DB path (overrides fills file).")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    context = resolve_account_context(args.account, env=env)
    db_path = args.db or _resolve_db_path()
    funding_result = None
    if db_path is not None and db_path.exists():
        trades, funding_result = _load_from_db(db_path, context)
    else:
        fills_path = args.fills_path or resolve_data_path(None, context, "fills.json")
        if not fills_path.exists():
            candidate = resolve_data_path(None, context, "fills.csv")
            fills_path = candidate if candidate.exists() else fills_path

        result = load_fills(fills_path, source=context.source, account_id=context.account_id)
        trades = reconstruct_trades(result.fills)
        if result.skipped:
            print(f"Skipped {result.skipped} fill rows during normalization.", file=sys.stderr)

    funding_path = args.funding
    if funding_path is None:
        default_funding = resolve_data_path(None, context, "funding.json")
        if default_funding.exists():
            funding_path = default_funding

    if funding_result is None and funding_path is not None:
        funding_result = load_funding(funding_path, source=context.source, account_id=context.account_id)

    if funding_result is not None:
        attributions = apply_funding_events(trades, funding_result.events)
        unmatched = sum(1 for item in attributions if item.matched_trade_id is None)
        if funding_result.skipped:
            print(
                f"Skipped {funding_result.skipped} funding rows during normalization.",
                file=sys.stderr,
            )
        if unmatched:
            print(f"Unmatched funding events: {unmatched}.", file=sys.stderr)

    app_config = load_app_config()
    # Canonical stored timeframe is 1m, but ApeX expects interval values like "1" (minutes) rather than "1m".
    apex_config = replace(PriceSeriesConfig.from_settings(app_config.pricing), interval="1")
    apex_price_client = ApexPriceClient(apex_config)
    hyperliquid_price_client = HyperliquidPriceClient(
        HyperliquidInfoConfig.from_env(env)
    )
    series_max_points = args.series_max_points
    if series_max_points is None:
        series_max_points = app_config.sync.series_max_points

    db_conn = None
    if db_path is not None:
        db_conn = sqlite_connect(db_path)
        init_db(db_conn)

    payload: dict[str, dict[str, float | None]] = {}
    series_payload: dict[str, list[dict[str, float | None]]] = {}
    try:
        for trade in trades:
            key = _trade_key(trade, local=args.local)
            try:
                bars = _bars_for_trade(
                    trade,
                    db_conn=db_conn,
                    apex_price_client=apex_price_client,
                    hyperliquid_price_client=hyperliquid_price_client,
                )
                apply_trade_excursions(trade, bars)
                payload[key] = {
                    "mae": trade.mae,
                    "mfe": trade.mfe,
                    "etd": trade.etd,
                }
                series = compute_trade_series(trade, bars)
                series = downsample_series(series, series_max_points)
                series_payload[key] = [
                    {
                        "t": int(point.timestamp.timestamp() * 1000),
                        "open": point.open,
                        "high": point.high,
                        "low": point.low,
                        "close": point.close,
                        "entry_return": point.entry_return,
                        "per_unit_unrealized": point.per_unit_unrealized,
                    }
                    for point in series
                ]
            except RuntimeError as exc:
                print(
                    f"Price series missing for {trade.symbol} "
                    f"{trade.entry_time.isoformat()} -> {trade.exit_time.isoformat()}: {exc}",
                    file=sys.stderr,
                )
                payload[key] = {
                    "mae": None,
                    "mfe": None,
                    "etd": None,
                }
                series_payload[key] = []
    finally:
        if db_conn is not None:
            db_conn.close()

    out_path = args.out
    if out_path is None:
        candidate = app_config.paths.excursions
        if candidate is not None:
            out_path = candidate
        else:
            out_path = resolve_data_path(None, context, "excursions.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    series_out = args.series_out
    if series_out is None:
        candidate = app_config.paths.trade_series
        if candidate is not None:
            series_out = candidate
        else:
            series_out = resolve_data_path(None, context, "trade_series.json")
    series_out.parent.mkdir(parents=True, exist_ok=True)
    series_out.write_text(json.dumps(series_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def _trade_key(trade, local: bool) -> str:
    entry = trade.entry_time.astimezone() if local else trade.entry_time
    exit_ = trade.exit_time.astimezone() if local else trade.exit_time
    parts = [
        trade.source,
        trade.account_id or "",
        trade.symbol,
        trade.side,
        entry.isoformat(),
        exit_.isoformat(),
        f"{trade.entry_price:.8f}",
        f"{trade.exit_price:.8f}",
        f"{trade.entry_size:.8f}",
        f"{trade.exit_size:.8f}",
        f"{trade.realized_pnl:.8f}",
    ]
    return "|".join(parts)


def _resolve_db_path() -> Path | None:
    app_config = load_app_config()
    return app_config.app.db_path


def _load_from_db(db_path: Path, context):
    conn = sqlite_reader.connect(db_path)
    try:
        fills = sqlite_reader.load_fills(conn, source=context.source, account_id=context.account_id)
        trades = reconstruct_trades(fills)
        funding_events = sqlite_reader.load_funding(conn, source=context.source, account_id=context.account_id)
        return trades, _FundingResult(funding_events)
    finally:
        conn.close()


class _FundingResult:
    def __init__(self, events):
        self.events = events
        self.skipped = 0


def _bars_for_trade(
    trade,
    *,
    db_conn,
    apex_price_client: ApexPriceClient,
    hyperliquid_price_client: HyperliquidPriceClient,
) -> list[PriceBar]:
    start = trade.entry_time - _CANONICAL_INTERVAL
    end = trade.exit_time + _CANONICAL_INTERVAL
    if db_conn is not None:
        cached = sqlite_reader.load_price_bars(
            db_conn,
            source=trade.source,
            symbol=trade.symbol,
            timeframe=_CANONICAL_TIMEFRAME,
            start=start,
            end=end,
        )
        cached_bars = _rows_to_bars(cached)
        if _covers_window(cached_bars, trade.entry_time, trade.exit_time):
            return cached_bars

    if trade.source == "hyperliquid":
        fetched = hyperliquid_price_client.fetch_bars(trade.symbol, trade.entry_time, trade.exit_time)
    elif trade.source == "apex":
        fetched = apex_price_client.fetch_bars(trade.symbol, trade.entry_time, trade.exit_time)
    else:
        raise RuntimeError(f"Unsupported price source: {trade.source}")

    if db_conn is not None:
        upsert_price_bars(db_conn, _bars_to_rows(trade.source, trade.symbol, fetched))
        merged = sqlite_reader.load_price_bars(
            db_conn,
            source=trade.source,
            symbol=trade.symbol,
            timeframe=_CANONICAL_TIMEFRAME,
            start=start,
            end=end,
        )
        merged_bars = _rows_to_bars(merged)
        if merged_bars:
            return merged_bars
    return fetched


def _bars_to_rows(source: str, symbol: str, bars: list[PriceBar]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for bar in bars:
        rows.append(
            {
                "source": source,
                "symbol": symbol,
                "timeframe": _CANONICAL_TIMEFRAME,
                "timestamp": bar.start_time.astimezone(timezone.utc).isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": None,
                "trade_count": None,
                "raw_json": {},
            }
        )
    return rows


def _rows_to_bars(rows: list[dict[str, object]]) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for row in rows:
        ts = row.get("timestamp")
        if not isinstance(ts, str):
            continue
        start = datetime.fromisoformat(ts)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        bars.append(
            PriceBar(
                start_time=start,
                end_time=start + _CANONICAL_INTERVAL,
                open=float(row.get("open") or 0.0),
                high=float(row.get("high") or 0.0),
                low=float(row.get("low") or 0.0),
                close=float(row.get("close") or 0.0),
            )
        )
    bars.sort(key=lambda item: item.start_time)
    return bars


def _covers_window(bars: list[PriceBar], start: datetime, end: datetime) -> bool:
    if not bars:
        return False
    earliest = bars[0].start_time
    latest = bars[-1].end_time
    return earliest <= start and latest >= end


if __name__ == "__main__":
    raise SystemExit(main())
