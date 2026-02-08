from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.75
DEFAULT_FILLS_PAGE_LIMIT = 2000
DEFAULT_FILLS_RECENT_CAP = 10000


@dataclass(frozen=True)
class HyperliquidInfoConfig:
    info_url: str
    timeout_seconds: float
    retry_attempts: int
    retry_backoff_seconds: float
    fills_page_limit: int
    fills_recent_cap: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "HyperliquidInfoConfig":
        return cls(
            info_url=str(env.get("HYPERLIQUID_INFO_URL", DEFAULT_INFO_URL)).strip() or DEFAULT_INFO_URL,
            timeout_seconds=_to_float(env.get("HYPERLIQUID_TIMEOUT_SECONDS"), DEFAULT_TIMEOUT_SECONDS),
            retry_attempts=int(env.get("HYPERLIQUID_RETRY_ATTEMPTS", str(DEFAULT_RETRY_ATTEMPTS))),
            retry_backoff_seconds=_to_float(
                env.get("HYPERLIQUID_RETRY_BACKOFF_SECONDS"), DEFAULT_RETRY_BACKOFF_SECONDS
            ),
            fills_page_limit=int(env.get("HYPERLIQUID_FILLS_PAGE_LIMIT", str(DEFAULT_FILLS_PAGE_LIMIT))),
            fills_recent_cap=int(env.get("HYPERLIQUID_FILLS_RECENT_CAP", str(DEFAULT_FILLS_RECENT_CAP))),
        )


class HyperliquidInfoClient:
    def __init__(self, config: HyperliquidInfoConfig) -> None:
        self._config = config

    @property
    def fills_page_limit(self) -> int:
        return self._config.fills_page_limit

    @property
    def fills_recent_cap(self) -> int:
        return self._config.fills_recent_cap

    def fetch_clearinghouse_state(self, user: str) -> Mapping[str, Any]:
        payload = {"type": "clearinghouseState", "user": user}
        response = self._post_info(payload)
        if not isinstance(response, Mapping):
            raise RuntimeError("Unexpected clearinghouseState response shape")
        return response

    def fetch_user_fills_by_time(
        self, *, user: str, start_ms: int, end_ms: int, aggregate_by_time: bool = False
    ) -> list[Mapping[str, Any]]:
        payload = {
            "type": "userFillsByTime",
            "user": user,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "aggregateByTime": bool(aggregate_by_time),
        }
        response = self._post_info(payload)
        if isinstance(response, list):
            return [row for row in response if isinstance(row, Mapping)]
        if isinstance(response, Mapping):
            data = response.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, Mapping)]
        return []

    def fetch_open_orders(self, user: str) -> list[Mapping[str, Any]]:
        payload = {"type": "openOrders", "user": user}
        response = self._post_info(payload)
        if isinstance(response, list):
            return [row for row in response if isinstance(row, Mapping)]
        if isinstance(response, Mapping):
            data = response.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, Mapping)]
        return []

    def fetch_historical_orders(self, user: str) -> list[Mapping[str, Any]]:
        payload = {"type": "historicalOrders", "user": user}
        response = self._post_info(payload)
        if isinstance(response, list):
            return [row for row in response if isinstance(row, Mapping)]
        if isinstance(response, Mapping):
            data = response.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, Mapping)]
        return []

    def fetch_order_status(self, *, user: str, oid: str) -> Mapping[str, Any] | None:
        payload = {"type": "orderStatus", "user": user, "oid": int(oid)}
        response = self._post_info(payload)
        if isinstance(response, Mapping):
            return response
        return None

    def _post_info(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | list[Any]:
        body = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        attempts = max(1, self._config.retry_attempts)
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(
                    self._config.info_url,
                    method="POST",
                    data=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                    raw = response.read()
                if not raw:
                    raise RuntimeError("Empty response body from Hyperliquid /info")
                return json.loads(raw.decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    break
                time.sleep(self._config.retry_backoff_seconds * (2**attempt))
        raise RuntimeError(f"Hyperliquid /info request failed: {last_error}") from last_error


def _to_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
