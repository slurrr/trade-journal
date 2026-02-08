from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11
    import tomli as tomllib


@dataclass(frozen=True)
class AppSettings:
    db_path: Path
    host: str
    port: int
    reload: bool
    env_path: Path


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    equity_endpoint: str
    timeout_seconds: float
    fills_limit: int
    retry_attempts: int
    retry_backoff_seconds: float
    debug: bool


@dataclass(frozen=True)
class PricingSettings:
    base_url: str
    endpoint: str
    interval: str
    timeout_seconds: float
    param_symbol: str
    param_interval: str
    param_start: str
    param_end: str
    time_unit: str
    symbol_transform: str
    max_bars: int


@dataclass(frozen=True)
class PathsSettings:
    excursions: Path | None
    trade_series: Path | None
    equity_history: Path | None


@dataclass(frozen=True)
class SyncSettings:
    auto_sync: bool
    interval_seconds: int
    overlap_hours: float
    limit: int | None
    max_pages: int
    end_ms: int | None
    run_excursions: bool
    series_max_points: int | None


@dataclass(frozen=True)
class SessionSettings:
    timezone: str
    auxiliary_windows: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class AnalyticsSettings:
    size_buckets: list[float]


@dataclass(frozen=True)
class AppConfig:
    app: AppSettings
    api: ApiSettings
    pricing: PricingSettings
    paths: PathsSettings
    sync: SyncSettings
    sessions: SessionSettings
    analytics: AnalyticsSettings


def apply_api_settings(
    env: Mapping[str, str],
    app_config: AppConfig,
    *,
    base_url_override: str | None = None,
) -> dict[str, str]:
    merged = dict(env)
    api = app_config.api
    merged["APEX_BASE_URL"] = base_url_override or api.base_url
    merged["APEX_EQUITY_ENDPOINT"] = api.equity_endpoint
    merged["APEX_TIMEOUT_SECONDS"] = str(api.timeout_seconds)
    merged["APEX_FILLS_LIMIT"] = str(api.fills_limit)
    merged["APEX_RETRY_ATTEMPTS"] = str(api.retry_attempts)
    merged["APEX_RETRY_BACKOFF_SECONDS"] = str(api.retry_backoff_seconds)
    merged["APEX_DEBUG"] = "true" if api.debug else "false"
    return merged


def load_app_config(path: Path | None = None) -> AppConfig:
    config_path = path or Path("config/app.toml")
    raw: Mapping[str, Any] = {}
    if config_path.exists():
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))

    app_raw = _section(raw, "app")
    api_raw = _section(raw, "api")
    pricing_raw = _section(raw, "pricing")
    paths_raw = _section(raw, "paths")
    sync_raw = _section(raw, "sync")
    sessions_raw = _section(raw, "sessions")
    analytics_raw = _section(raw, "analytics")

    app = AppSettings(
        db_path=Path(app_raw.get("db_path", "data/trade_journal.sqlite")),
        host=str(app_raw.get("host", "127.0.0.1")),
        port=int(app_raw.get("port", 8000)),
        reload=bool(app_raw.get("reload", True)),
        env_path=Path(app_raw.get("env_path", ".env")),
    )

    api = ApiSettings(
        base_url=str(api_raw.get("base_url", "https://omni.apex.exchange")),
        equity_endpoint=str(api_raw.get("equity_endpoint", "/v3/yesterday-pnl")),
        timeout_seconds=float(api_raw.get("timeout_seconds", 30.0)),
        fills_limit=int(api_raw.get("fills_limit", 100)),
        retry_attempts=int(api_raw.get("retry_attempts", 3)),
        retry_backoff_seconds=float(api_raw.get("retry_backoff_seconds", 0.75)),
        debug=bool(api_raw.get("debug", False)),
    )

    pricing = PricingSettings(
        base_url=str(pricing_raw.get("base_url", "https://omni.apex.exchange/api")),
        endpoint=str(pricing_raw.get("endpoint", "/v3/klines")),
        interval=str(pricing_raw.get("interval", "1")),
        timeout_seconds=float(pricing_raw.get("timeout_seconds", 30.0)),
        param_symbol=str(pricing_raw.get("param_symbol", "symbol")),
        param_interval=str(pricing_raw.get("param_interval", "interval")),
        param_start=str(pricing_raw.get("param_start", "start")),
        param_end=str(pricing_raw.get("param_end", "end")),
        time_unit=str(pricing_raw.get("time_unit", "s")),
        symbol_transform=str(pricing_raw.get("symbol_transform", "strip-dash")),
        max_bars=int(pricing_raw.get("max_bars", 200)),
    )

    paths = PathsSettings(
        excursions=_path_or_none(paths_raw.get("excursions")),
        trade_series=_path_or_none(paths_raw.get("trade_series")),
        equity_history=_path_or_none(paths_raw.get("equity_history")),
    )

    sync = SyncSettings(
        auto_sync=bool(sync_raw.get("auto_sync", True)),
        interval_seconds=int(sync_raw.get("interval_seconds", 900)),
        overlap_hours=float(sync_raw.get("overlap_hours", 48.0)),
        limit=_int_or_none(sync_raw.get("limit")),
        max_pages=int(sync_raw.get("max_pages", 200)),
        end_ms=_int_or_none(sync_raw.get("end_ms")),
        run_excursions=bool(sync_raw.get("run_excursions", True)),
        series_max_points=_int_or_none(sync_raw.get("series_max_points")),
    )

    aux_windows = _parse_auxiliary_windows(sessions_raw.get("auxiliary_windows"))
    if not aux_windows:
        aux_windows = _default_auxiliary_windows()

    sessions = SessionSettings(
        timezone=str(sessions_raw.get("timezone", "utc")).strip().lower() or "utc",
        auxiliary_windows=aux_windows,
    )

    analytics = AnalyticsSettings(
        size_buckets=_float_list(analytics_raw.get("size_buckets")) or _default_size_buckets(),
    )

    return AppConfig(
        app=app,
        api=api,
        pricing=pricing,
        paths=paths,
        sync=sync,
        sessions=sessions,
        analytics=analytics,
    )


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if isinstance(value, Mapping):
        return value
    return {}


def _int_or_none(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _path_or_none(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(str(value))


def _float_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    output: list[float] = []
    for item in value:
        try:
            output.append(float(item))
        except (TypeError, ValueError):
            continue
    return output


def _default_size_buckets() -> list[float]:
    return [1_000.0, 5_000.0, 10_000.0, 25_000.0, 50_000.0]


def _default_auxiliary_windows() -> dict[str, tuple[int, int]]:
    return {
        "tokyo_london_overlap": (_minutes("08:00"), _minutes("09:59", inclusive_end=True)),
        "london_ny_overlap": (_minutes("16:00"), _minutes("17:59", inclusive_end=True)),
    }


def _parse_auxiliary_windows(raw: Any) -> dict[str, tuple[int, int]]:
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, tuple[int, int]] = {}
    for name, value in raw.items():
        if not name:
            continue
        normalized = _normalize_window_name(name)
        window = _parse_time_window(value)
        if window is None:
            continue
        if window[0] == window[1]:
            continue
        output[normalized] = window
    return output


def _parse_time_window(value: Any) -> tuple[int, int] | None:
    if isinstance(value, Mapping):
        start = value.get("start")
        end = value.get("end")
        if isinstance(start, str) and isinstance(end, str):
            try:
                start_min = _minutes(start)
                end_min = _minutes(end, inclusive_end=True)
            except ValueError:
                return None
            return start_min, end_min
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
        if isinstance(start, str) and isinstance(end, str):
            try:
                start_min = _minutes(start)
                end_min = _minutes(end, inclusive_end=True)
            except ValueError:
                return None
            return start_min, end_min
    if isinstance(value, str) and "-" in value:
        start, end = value.split("-", 1)
        try:
            start_min = _minutes(start.strip())
            end_min = _minutes(end.strip(), inclusive_end=True)
        except ValueError:
            return None
        return start_min, end_min
    return None


def _minutes(value: str, *, inclusive_end: bool = False) -> int:
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        raise ValueError(f"Invalid time value: {value}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid time value: {value}")
    total = hour * 60 + minute
    if inclusive_end:
        total += 1
    return total


def _normalize_window_name(value: str) -> str:
    cleaned = str(value).strip().lower()
    cleaned = cleaned.replace("-", "_").replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9_]+", "", cleaned)
    return cleaned or "window"
