from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Probe ApeX Omni REST endpoints with signing.")
    parser.add_argument(
        "paths",
        nargs="+",
        help="Endpoint path(s) like /v3/history-orders (query params via --param).",
    )
    parser.add_argument("--method", type=str, default="GET", help="HTTP method (default GET).")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Query/body param as key=value. Can be specified multiple times.",
    )
    parser.add_argument("--env", type=Path, default=app_config.app.env_path, help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument("--no-auth", action="store_true", help="Do not send API auth headers.")
    args = parser.parse_args(argv)

    if args.no_auth:
        client = _PublicClient(args.base_url, args.env, app_config)
    else:
        env = dict(os.environ)
        env.update(load_dotenv(args.env))
        env = apply_api_settings(env, app_config, base_url_override=args.base_url)
        config = ApexApiConfig.from_env(env)
        client = ApexApiClient(config)

    params = _parse_params(args.param)
    results: dict[str, Any] = {}
    for path in args.paths:
        normalized = path if path.startswith("/") else f"/{path}"
        results[normalized] = client._request(args.method.upper(), normalized, params=params)  # type: ignore

    if len(results) == 1:
        payload: Any = next(iter(results.values()))
    else:
        payload = results

    if args.pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload))
    return 0


def _parse_params(raw_params: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw_params:
        if "=" not in item:
            raise ValueError(f"Invalid param {item!r}; expected key=value.")
        key, value = item.split("=", 1)
        params[key] = value
    return params


class _PublicClient:
    def __init__(self, base_url: str | None, env_path: Path, app_config) -> None:
        base = base_url or app_config.api.base_url
        self._base_url = base.rstrip("/")

    def _request(self, method: str, path: str, params: dict[str, str] | None = None) -> Any:
        import urllib.parse
        import urllib.request

        params = dict(params or {})
        query = urllib.parse.urlencode(params)
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method=method)
        with urllib.request.urlopen(request) as response:
            payload = response.read()
        return json.loads(payload.decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
