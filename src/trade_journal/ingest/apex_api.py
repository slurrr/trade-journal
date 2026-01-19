from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_BASE_URL = "https://omni.apex.exchange"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_FILLS_LIMIT = 100
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.75


@dataclass(frozen=True)
class ApexApiConfig:
    base_url: str
    api_key: str
    api_secret: str
    api_passphrase: str
    debug: bool
    timeout_seconds: float
    fills_limit: int
    retry_attempts: int
    retry_backoff_seconds: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ApexApiConfig":
        api_key = env.get("APEX_API_KEY", "").strip()
        api_secret = env.get("APEX_API_SECRET", "").strip()
        api_passphrase = env.get("APEX_API_PASSPHRASE", "").strip() or env.get("APEX_PASSPHRASE", "").strip()

        missing = [
            name
            for name, value in (
                ("APEX_API_KEY", api_key),
                ("APEX_API_SECRET", api_secret),
                ("APEX_API_PASSPHRASE", api_passphrase),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required environment values: {', '.join(missing)}")

        base_url = _normalize_base_url(env.get("APEX_BASE_URL", DEFAULT_BASE_URL))
        timeout_seconds = _to_float(env.get("APEX_TIMEOUT_SECONDS"), default=DEFAULT_TIMEOUT_SECONDS)
        fills_limit = int(env.get("APEX_FILLS_LIMIT", str(DEFAULT_FILLS_LIMIT)))
        retry_attempts = int(env.get("APEX_RETRY_ATTEMPTS", str(DEFAULT_RETRY_ATTEMPTS)))
        retry_backoff_seconds = _to_float(
            env.get("APEX_RETRY_BACKOFF_SECONDS"), default=DEFAULT_RETRY_BACKOFF_SECONDS
        )

        return cls(
            base_url=base_url,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            debug=env.get("APEX_DEBUG", "").lower() in {"1", "true", "yes"},
            timeout_seconds=timeout_seconds,
            fills_limit=fills_limit,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
        )


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip().strip("\"").strip("'")
    return env


class ApexApiClient:
    def __init__(self, config: ApexApiConfig) -> None:
        self._config = config

    @property
    def debug(self) -> bool:
        return bool(self._config.debug)

    def fetch_fills(
        self,
        limit: int | None = None,
        page: int = 0,
        begin_ms: int | None = None,
        end_ms: int | None = None,
    ) -> Mapping[str, Any]:
        params = {
            "limit": str(limit if limit is not None else self._config.fills_limit),
            "page": str(page),
        }
        if begin_ms is not None:
            params["beginTimeInclusive"] = str(begin_ms)
        if end_ms is not None:
            params["endTimeExclusive"] = str(end_ms)
        return self._request("GET", "/v3/fills", params=params)

    def fetch_historical_pnl(self, limit: int | None = None, page: int = 0) -> Mapping[str, Any]:
        params = {
            "limit": str(limit if limit is not None else self._config.fills_limit),
            "page": str(page),
        }
        return self._request("GET", "/v3/historical-pnl", params=params)

    def fetch_history_orders(self, limit: int | None = None, page: int = 0) -> Mapping[str, Any]:
        params = {
            "limit": str(limit if limit is not None else self._config.fills_limit),
            "page": str(page),
        }
        return self._request("GET", "/v3/history-orders", params=params)

    def fetch_funding(
        self,
        limit: int | None = None,
        page: int = 0,
        begin_ms: int | None = None,
        end_ms: int | None = None,
    ) -> Mapping[str, Any]:
        params = {
            "limit": str(limit if limit is not None else self._config.fills_limit),
            "page": str(page),
        }
        if begin_ms is not None:
            params["beginTimeInclusive"] = str(begin_ms)
        if end_ms is not None:
            params["endTimeExclusive"] = str(end_ms)
        return self._request("GET", "/v3/funding", params=params)

    def _request(self, method: str, path: str, params: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        params = dict(params or {})
        query = _encode_query(params)
        url = f"{self._config.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        signature_paths = _signature_path_variants(self._config.base_url, path)
        timestamp = _timestamp_ms()

        if method.upper() == "GET":
            signed_variants = []
            for signature_path in signature_paths:
                signed_variants.extend(_get_signature_variants(signature_path, query))
        else:
            signed_variants = [(signature_path, _encode_form(params)) for signature_path in signature_paths]

        secret = _secret_base64_raw(self._config.api_secret)
        last_error: Mapping[str, Any] | None = None
        for signed_path, data_string in signed_variants:
            if self.debug:
                print(
                    f"apex debug: url={url} signed_path={signed_path} data_string={data_string!r} "
                    f"timestamp={timestamp}",
                )
                print("apex debug: secret_mode=base64(raw)")
            signature = _sign_request(
                secret=secret,
                timestamp=timestamp,
                method=method,
                path=signed_path,
                data_string=data_string,
                signature_encoding="base64",
            )

            payload = _send_with_retry(
                url=url,
                method=method,
                api_key=self._config.api_key,
                passphrase=self._config.api_passphrase,
                signature=signature,
                timestamp=timestamp,
                timeout_seconds=self._config.timeout_seconds,
                data_string=data_string if method.upper() != "GET" else None,
                attempts=self._config.retry_attempts,
                backoff_seconds=self._config.retry_backoff_seconds,
            )

            if _is_signature_error(payload):
                if self.debug:
                    print(f"apex debug: signature failed with {payload}")
                last_error = payload
                continue

            return payload

        code = last_error.get("code") if last_error else None
        msg = last_error.get("msg") if last_error else None
        raise RuntimeError(f"Signature check failed (code={code}, msg={msg}).")


def _send_request(
    url: str,
    method: str,
    api_key: str,
    passphrase: str,
    signature: str,
    timestamp: str,
    timeout_seconds: float,
    data_string: str | None,
) -> Mapping[str, Any]:
    headers = {
        "APEX-API-KEY": api_key,
        "APEX-PASSPHRASE": passphrase,
        "APEX-TIMESTAMP": timestamp,
        "APEX-SIGNATURE": signature,
        "Accept": "application/json",
    }

    data_bytes = data_string.encode("utf-8") if data_string is not None else None
    if data_bytes is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, headers=headers, data=data_bytes, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc

    if not payload:
        raise RuntimeError(
            f"Empty response body (status {status}, content-type {content_type}, url {url})"
        )

    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        snippet = payload[:500].decode("utf-8", errors="replace")
        hint = ""
        if "text/html" in content_type.lower():
            hint = " (HTML response: check APEX_BASE_URL points to the API host, not the web app)"
        raise RuntimeError(
            f"Non-JSON response (status {status}, content-type {content_type}, url {url}){hint}: {snippet}"
        ) from exc


def _send_with_retry(
    url: str,
    method: str,
    api_key: str,
    passphrase: str,
    signature: str,
    timestamp: str,
    timeout_seconds: float,
    data_string: str | None,
    attempts: int,
    backoff_seconds: float,
) -> Mapping[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _send_request(
                url=url,
                method=method,
                api_key=api_key,
                passphrase=passphrase,
                signature=signature,
                timestamp=timestamp,
                timeout_seconds=timeout_seconds,
                data_string=data_string,
            )
        except Exception as exc:  # noqa: BLE001 - we only rethrow after retry policy
            last_error = exc
            if not _should_retry(exc, attempt, attempts):
                raise
            sleep_seconds = backoff_seconds * (2**attempt)
            time.sleep(sleep_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Request retry loop exited without sending.")


# NOTE: Signature payload follows ApeX docs for REST API key auth.

def _sign_request(
    secret: bytes,
    timestamp: str,
    method: str,
    path: str,
    data_string: str,
    signature_encoding: str,
) -> str:
    message = f"{timestamp}{method.upper()}{path}{data_string}"
    digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
    if signature_encoding == "hex":
        return digest.hex()
    if signature_encoding != "base64":
        raise ValueError("Signature encoding must be 'base64' or 'hex'")
    return base64.b64encode(digest).decode("utf-8")


def _signature_path(base_url: str, path: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    base_path = parsed.path.rstrip("/")
    if base_path:
        return f"{base_path}{path}"
    return path


def _signature_path_variants(base_url: str, path: str) -> list[str]:
    return [_signature_path(base_url, path)]


def _normalize_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    base_path = parsed.path.rstrip("/")
    if not base_path.endswith("/api"):
        base_path = f"{base_path}/api" if base_path else "/api"
    normalized = parsed._replace(path=base_path)
    return urllib.parse.urlunparse(normalized).rstrip("/")


def _append_query(path: str, query: str) -> str:
    if not query:
        return path
    return f"{path}?{query}"


def _get_signature_variants(path: str, query: str) -> list[tuple[str, str]]:
    if not query:
        return [(path, "")]
    return [(_append_query(path, query), "")]


def _encode_query(params: Mapping[str, str]) -> str:
    return urllib.parse.urlencode(list(params.items()))


def _encode_form(params: Mapping[str, str]) -> str:
    if not params:
        return ""
    return "&".join(f"{key}={value}" for key, value in sorted(params.items()))


def _secret_base64_raw(secret: str) -> bytes:
    return base64.standard_b64encode(secret.encode("utf-8"))


def _timestamp_ms() -> str:
    return str(int(time.time() * 1000))


def _is_signature_error(payload: Mapping[str, Any]) -> bool:
    code = payload.get("code") if isinstance(payload, dict) else None
    if code is None:
        return False
    return str(code) == "20016"


def _to_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _should_retry(exc: Exception, attempt: int, attempts: int) -> bool:
    if attempt >= attempts - 1:
        return False
    message = str(exc).lower()
    if "non-json response" in message:
        return False
    if "signature" in message:
        return False
    if "empty response body" in message:
        return True
    if "timed out" in message:
        return True
    if "temporary failure" in message:
        return True
    if "http 429" in message:
        return True
    if "http 5" in message:
        return True
    if "http 408" in message:
        return True
    if "http 502" in message or "http 503" in message or "http 504" in message:
        return True
    return False
