from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from trade_journal.config.accounts import resolve_account_context, resolve_data_path
from trade_journal.config.app_config import apply_api_settings, load_app_config
from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv


def main(argv: list[str] | None = None) -> int:
    app_config = load_app_config()
    parser = argparse.ArgumentParser(description="Fetch ApeX Omni account info (positions/balance).")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON response.")
    parser.add_argument("--env", type=Path, default=app_config.app.env_path, help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--account", type=str, default=None, help="Account name from accounts config.")
    parser.add_argument("--out", type=Path, default=None, help="Output file.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    env = apply_api_settings(env, app_config, base_url_override=args.base_url)
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)
    context = resolve_account_context(args.account, env=env)

    payload = client.fetch_account()
    text = json.dumps(payload, indent=2, sort_keys=True)

    if args.raw:
        print(text)
        return 0

    out_path = args.out or resolve_data_path(None, context, "account.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
