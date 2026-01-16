from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from trade_journal.metrics.excursions import PriceBar

# 1m candles keep intrabar ambiguity low without pulling excessive data.
DEFAULT_PRICE_INTERVAL = "1m"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TIME_UNIT = "ms"
DEFAULT_SYMBOL_TRANSFORM = "none"
DEFAULT_MAX_BARS_PER_REQUEST = 500


@dataclass(frozen=True)
class PriceSeriesConfig:
    base_url: str
    endpoint: str
    interval: str
    timeout_seconds: float
    symbol_param: str
    interval_param: str
    start_param: str
    end_param: str
    time_unit: str
    symbol_transform: str
    max_bars_per_request: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "PriceSeriesConfig":
        base_url = env.get("APEX_PRICE_BASE_URL", "").strip()
        endpoint = env.get("APEX_PRICE_ENDPOINT", "").strip()
        if not base_url or not endpoint:
            raise ValueError("APEX_PRICE_BASE_URL and APEX_PRICE_ENDPOINT must be set to fetch prices.")

        interval = env.get("APEX_PRICE_INTERVAL", DEFAULT_PRICE_INTERVAL).strip() or DEFAULT_PRICE_INTERVAL
        timeout_seconds = _to_float(env.get("APEX_PRICE_TIMEOUT_SECONDS"), default=DEFAULT_TIMEOUT_SECONDS)
        symbol_param = env.get("APEX_PRICE_PARAM_SYMBOL", "symbol").strip() or "symbol"
        interval_param = env.get("APEX_PRICE_PARAM_INTERVAL", "interval").strip() or "interval"
        start_param = env.get("APEX_PRICE_PARAM_START", "startTime").strip() or "startTime"
        end_param = env.get("APEX_PRICE_PARAM_END", "endTime").strip() or "endTime"
        time_unit = env.get("APEX_PRICE_TIME_UNIT", DEFAULT_TIME_UNIT).strip().lower() or DEFAULT_TIME_UNIT
        symbol_transform = (
            env.get("APEX_PRICE_SYMBOL_TRANSFORM", DEFAULT_SYMBOL_TRANSFORM).strip().lower()
            or DEFAULT_SYMBOL_TRANSFORM
        )
        max_bars_per_request = int(
            env.get("APEX_PRICE_MAX_BARS", str(DEFAULT_MAX_BARS_PER_REQUEST)).strip()
            or DEFAULT_MAX_BARS_PER_REQUEST
        )

        return cls(
            base_url=base_url.rstrip("/"),
            endpoint=endpoint,
            interval=interval,
            timeout_seconds=timeout_seconds,
            symbol_param=symbol_param,
            interval_param=interval_param,
            start_param=start_param,
            end_param=end_param,
            time_unit=time_unit,
            symbol_transform=symbol_transform,
            max_bars_per_request=max_bars_per_request,
        )


class ApexPriceClient:
    def __init__(self, config: PriceSeriesConfig) -> None:
        self._config = config

    def fetch_bars(self, symbol: str, start_time: datetime, end_time: datetime) -> list[PriceBar]:
        interval_ms = _interval_to_ms(self._config.interval)
        # Expand the window by one interval to capture the entry/exit bars.
        start_ms = int(start_time.timestamp() * 1000) - interval_ms
        end_ms = int(end_time.timestamp() * 1000) + interval_ms
        bars = self._fetch_bars_window(symbol, start_ms, end_ms, interval_ms)
        if not bars:
            raise RuntimeError(f"No price data returned for {symbol}.")
        _ensure_coverage(bars, start_time, end_time)
        return bars

    def _fetch_bars_window(self, symbol: str, start_ms: int, end_ms: int, interval_ms: int) -> list[PriceBar]:
        max_bars = max(1, self._config.max_bars_per_request)
        chunk_ms = interval_ms * max_bars
        cursor_ms = start_ms
        bars: list[PriceBar] = []

        while cursor_ms < end_ms:
            chunk_end = min(end_ms, cursor_ms + chunk_ms)
            payload = self._request_bars(symbol, cursor_ms, chunk_end)
            chunk_bars = _normalize_bars(payload, interval_ms)
            if not chunk_bars:
                summary = _describe_payload(payload)
                raise RuntimeError(f"No price data returned for {symbol}. {summary}")
            bars.extend(chunk_bars)
            cursor_ms = chunk_end

        bars.sort(key=lambda bar: bar.start_time)
        return bars

    def _request_bars(self, symbol: str, start_ms: int, end_ms: int) -> Any:
        start_value = _format_time_value(start_ms, self._config.time_unit)
        end_value = _format_time_value(end_ms, self._config.time_unit)
        params = {
            self._config.symbol_param: _transform_symbol(symbol, self._config.symbol_transform),
            self._config.interval_param: self._config.interval,
            self._config.start_param: start_value,
            self._config.end_param: end_value,
        }
        url = _build_url(self._config.base_url, self._config.endpoint, params)
        return _fetch_json(url, timeout_seconds=self._config.timeout_seconds)


def _build_url(base_url: str, endpoint: str, params: Mapping[str, str]) -> str:
    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    query = urllib.parse.urlencode(params)
    return f"{base_url}{path}?{query}"


def _fetch_json(url: str, timeout_seconds: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read()
    if not payload:
        raise RuntimeError(f"Empty response from price endpoint: {url}")
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        snippet = payload[:500].decode("utf-8", errors="replace")
        raise RuntimeError(f"Non-JSON price response: {snippet}") from exc


def _normalize_bars(payload: Any, interval_ms: int) -> list[PriceBar]:
    records: Iterable[Any] = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("data", "bars", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
            if isinstance(value, dict):
                if value and all(isinstance(item, list) for item in value.values()):
                    records = [item for sublist in value.values() for item in sublist]
                    break
                for nested_key in ("list", "klines", "candles", "records"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        records = nested
                        break
                if records:
                    break
        if not records and _looks_like_bar(payload):
            records = [payload]
    bars: list[PriceBar] = []
    for record in records:
        bar = _parse_bar(record, interval_ms)
        if bar is not None:
            bars.append(bar)
    bars.sort(key=lambda bar: bar.start_time)
    return bars


def _parse_bar(record: Any, interval_ms: int) -> PriceBar | None:
    if isinstance(record, Mapping):
        timestamp = _pick(record, "startTime", "timestamp", "time", "t")
        open_ = _pick(record, "open", "o")
        high = _pick(record, "high", "h")
        low = _pick(record, "low", "l")
        close = _pick(record, "close", "c")
    elif isinstance(record, (list, tuple)) and len(record) >= 5:
        timestamp, open_, high, low, close = record[:5]
    else:
        return None

    if timestamp is None:
        return None

    start_time = _parse_timestamp(timestamp)
    end_time = start_time + timedelta(milliseconds=interval_ms)
    return PriceBar(
        start_time=start_time,
        end_time=end_time,
        open=_to_float(open_),
        high=_to_float(high),
        low=_to_float(low),
        close=_to_float(close),
    )


def _ensure_coverage(bars: list[PriceBar], start_time: datetime, end_time: datetime) -> None:
    earliest = bars[0].start_time
    latest = bars[-1].end_time
    if earliest > start_time or latest < end_time:
        raise RuntimeError(
            "Price data does not fully cover trade window: "
            f"{earliest.isoformat()} -> {latest.isoformat()} "
            f"(needed {start_time.isoformat()} -> {end_time.isoformat()})"
        )


def _interval_to_ms(interval: str) -> int:
    text = interval.strip().lower()
    if text.isdigit():
        return int(text) * 60_000
    if text.endswith("m"):
        return int(text[:-1]) * 60_000
    if text.endswith("h"):
        return int(text[:-1]) * 3_600_000
    if text.endswith("d"):
        return int(text[:-1]) * 86_400_000
    raise ValueError(f"Unsupported price interval: {interval}")


def _pick(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _parse_timestamp(value: Any) -> datetime:
    if value is None:
        raise ValueError("Missing timestamp")
    if isinstance(value, (int, float)):
        return _timestamp_from_number(float(value))
    text = str(value).strip()
    try:
        numeric = float(text)
        return _timestamp_from_number(numeric)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_from_number(value: float) -> datetime:
    seconds = value / 1000.0 if value > 1e12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _to_float(value: Any, default: float | None = None) -> float:
    if value is None:
        if default is None:
            raise ValueError("Missing numeric field")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid numeric field") from exc


def _format_time_value(value_ms: int, unit: str) -> str:
    if unit == "ms":
        return str(value_ms)
    if unit == "s":
        return str(int(value_ms / 1000))
    raise ValueError(f"Unsupported time unit: {unit}")


def _transform_symbol(symbol: str, transform: str) -> str:
    if transform == "strip-dash":
        return symbol.replace("-", "")
    if transform == "none":
        return symbol
    raise ValueError(f"Unsupported symbol transform: {transform}")


def _looks_like_bar(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in ("t", "time", "timestamp", "startTime"))


def _describe_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = sorted(payload.keys())
        data = payload.get("data")
        detail = ""
        if isinstance(data, dict):
            data_keys = sorted(data.keys())
            list_counts = {}
            for key in data_keys:
                value = data.get(key)
                if isinstance(value, list):
                    list_counts[key] = len(value)
            if list_counts:
                detail = f" data_lists={list_counts}"
            else:
                detail = f" data_keys={data_keys}"
        return f"response_keys={keys}{detail}"
    if isinstance(payload, list):
        return f"response_list_len={len(payload)}"
    return f"response_type={type(payload).__name__}"
