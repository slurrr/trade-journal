from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from trade_journal.ingest.apex_api import ApexApiClient, ApexApiConfig, load_dotenv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch ApeX Omni account info (positions/balance).")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON response.")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file.")
    parser.add_argument("--base-url", type=str, default=None, help="Override APEX_BASE_URL.")
    parser.add_argument("--out", type=Path, default=Path("data/account.json"), help="Output file.")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env.update(load_dotenv(args.env))
    if args.base_url:
        env["APEX_BASE_URL"] = args.base_url
    config = ApexApiConfig.from_env(env)
    client = ApexApiClient(config)

    payload = client.fetch_account()
    text = json.dumps(payload, indent=2, sort_keys=True)

    if args.raw:
        print(text)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
