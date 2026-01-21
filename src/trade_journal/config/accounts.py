from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11
    import tomli as tomllib


@dataclass(frozen=True)
class AccountConfig:
    name: str
    source: str
    account_id: str | None
    data_dir: Path
    funding_baseline: str | None


@dataclass(frozen=True)
class AccountsConfig:
    default_account: str | None
    accounts: dict[str, AccountConfig]


@dataclass(frozen=True)
class AccountContext:
    name: str
    source: str
    account_id: str | None
    data_dir: Path
    funding_baseline: str | None


def load_accounts_config(path: Path) -> AccountsConfig:
    if not path.exists():
        return AccountsConfig(default_account=None, accounts={})
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    default_account = raw.get("default_account")
    accounts_block = raw.get("accounts", {}) if isinstance(raw, dict) else {}
    accounts: dict[str, AccountConfig] = {}
    for name, cfg in accounts_block.items():
        if not isinstance(cfg, Mapping):
            continue
        source = str(cfg.get("source") or "apex").strip().lower()
        account_id = cfg.get("account_id") or cfg.get("accountId")
        data_dir = cfg.get("data_dir") or cfg.get("dataDir") or f"data/{name}"
        funding_baseline = cfg.get("funding_baseline") or cfg.get("fundingBaseline")
        accounts[name] = AccountConfig(
            name=name,
            source=source,
            account_id=str(account_id) if account_id else None,
            data_dir=Path(data_dir),
            funding_baseline=str(funding_baseline) if funding_baseline else None,
        )
    if default_account and default_account not in accounts:
        raise ValueError(f"Default account '{default_account}' not found in accounts config.")
    return AccountsConfig(default_account=default_account, accounts=accounts)


def resolve_account_context(
    account_name: str | None = None,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> AccountContext:
    env = env or os.environ
    config_path = Path(
        config_path
        or env.get("TRADE_JOURNAL_ACCOUNTS_CONFIG", "config/accounts.toml")
    )
    config = load_accounts_config(config_path)
    resolved_name = (
        account_name
        or env.get("TRADE_JOURNAL_ACCOUNT_NAME")
        or config.default_account
        or (next(iter(config.accounts)) if config.accounts else "default")
    )
    if config.accounts:
        if resolved_name not in config.accounts:
            raise ValueError(f"Unknown account '{resolved_name}'.")
        account = config.accounts[resolved_name]
        return AccountContext(
            name=account.name,
            source=account.source,
            account_id=account.account_id or account.name,
            data_dir=account.data_dir,
            funding_baseline=account.funding_baseline,
        )
    data_dir = Path(env.get("TRADE_JOURNAL_DATA_DIR", "data"))
    source = env.get("TRADE_JOURNAL_SOURCE", "apex")
    account_id = env.get("TRADE_JOURNAL_ACCOUNT_ID")
    return AccountContext(
        name=resolved_name,
        source=source,
        account_id=account_id if account_id else resolved_name,
        data_dir=data_dir,
        funding_baseline=None,
    )


def resolve_data_path(
    override: str | None, context: AccountContext, filename: str
) -> Path:
    if override:
        return Path(override)
    return context.data_dir / filename
