from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from trade_journal.ingest.hyperliquid_api import HyperliquidInfoClient, HyperliquidInfoConfig
from trade_journal.metrics.excursions import PriceBar


_MINUTE_MS = 60_000


class HyperliquidPriceClient:
    def __init__(self, config: HyperliquidInfoConfig) -> None:
        self._client = HyperliquidInfoClient(config)

    def fetch_bars(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
        timeframe: str = "1m",
    ) -> list[PriceBar]:
        step_ms = _timeframe_to_ms(timeframe)
        coin = _symbol_to_coin(symbol)
        start_ms = int(start_time.timestamp() * 1000) - step_ms
        end_ms = int(end_time.timestamp() * 1000) + step_ms
        start_ms = _floor_ms(start_ms, step_ms)
        end_ms = _ceil_ms(end_ms, step_ms)
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": timeframe,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
            },
        }
        response = self._client._post_info(payload)  # noqa: SLF001 - internal helper is stable here.
        bars = _parse_candle_snapshot(response)
        if not bars:
            summary = _describe_payload(response)
            raise RuntimeError(f"No candleSnapshot data returned for {symbol}. {summary}")
        bars.sort(key=lambda bar: bar.start_time)
        _ensure_coverage(bars, start_time, end_time, timeframe=timeframe)
        return bars


def _symbol_to_coin(symbol: str) -> str:
    text = str(symbol).strip().upper()
    if text.endswith("-USDC"):
        return text[:-5]
    if text.endswith("-USDT"):
        return text[:-5]
    return text.replace("-", "")


def _parse_candle_snapshot(payload: Any) -> list[PriceBar]:
    records: Iterable[Any] = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, Mapping):
        for key in ("data", "candles", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
        if not records and _looks_like_candle(payload):
            records = [payload]

    bars: list[PriceBar] = []
    for item in records:
        bar = _parse_bar(item)
        if bar is not None:
            bars.append(bar)
    return bars


def _parse_bar(item: Any) -> PriceBar | None:
    if isinstance(item, Mapping):
        start_raw = _pick(item, "t", "time", "startTime", "timestamp")
        end_raw = _pick(item, "T", "endTime")
        open_raw = _pick(item, "o", "open")
        high_raw = _pick(item, "h", "high")
        low_raw = _pick(item, "l", "low")
        close_raw = _pick(item, "c", "close")
    elif isinstance(item, (list, tuple)) and len(item) >= 6:
        start_raw, end_raw, open_raw, high_raw, low_raw, close_raw = item[:6]
    else:
        return None

    if start_raw is None:
        return None
    start_time = _parse_ts(start_raw)
    if end_raw is None:
        end_time = start_time + timedelta(minutes=1)
    else:
        end_time = _parse_ts(end_raw)
        if end_time <= start_time:
            end_time = start_time + timedelta(minutes=1)
    return PriceBar(
        start_time=start_time,
        end_time=end_time,
        open=float(open_raw),
        high=float(high_raw),
        low=float(low_raw),
        close=float(close_raw),
    )


def _pick(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    try:
        numeric = float(text)
    except ValueError:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    if numeric > 1e12:
        numeric /= 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def _looks_like_candle(value: Mapping[str, Any]) -> bool:
    return _pick(value, "o", "open") is not None and _pick(value, "c", "close") is not None


def _floor_ms(value: int, step_ms: int) -> int:
    if step_ms <= 0:
        return value
    return value - (value % step_ms)


def _ceil_ms(value: int, step_ms: int) -> int:
    if step_ms <= 0:
        return value
    rem = value % step_ms
    return value if rem == 0 else value + (step_ms - rem)


def _describe_payload(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return f"response_keys={sorted(payload.keys())}"
    if isinstance(payload, list):
        return f"response_list_len={len(payload)}"
    return f"response_type={type(payload).__name__}"


def _ensure_coverage(
    bars: list[PriceBar],
    start_time: datetime,
    end_time: datetime,
    *,
    timeframe: str,
) -> None:
    earliest = bars[0].start_time
    latest = bars[-1].end_time
    if earliest > start_time or latest < end_time:
        raise RuntimeError(
            "Price data does not fully cover trade window: "
            f"{earliest.isoformat()} -> {latest.isoformat()} "
            f"(needed {start_time.isoformat()} -> {end_time.isoformat()} @ {timeframe})"
        )


def _timeframe_to_ms(timeframe: str) -> int:
    text = str(timeframe).strip().lower()
    if text.endswith("m"):
        minutes = int(text[:-1] or "1")
        return max(1, minutes) * _MINUTE_MS
    raise ValueError(f"Unsupported timeframe: {timeframe}")
