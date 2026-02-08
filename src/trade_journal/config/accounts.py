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
    exchange: str | None
    base_currency: str | None
    starting_equity: float | None
    active: bool


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
    exchange: str | None
    base_currency: str | None
    starting_equity: float | None
    active: bool


def account_key(source: str, account_id: str | None) -> str:
    return f"{source}:{account_id or ''}"


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
        exchange = cfg.get("exchange")
        base_currency = cfg.get("base_currency") or cfg.get("baseCurrency")
        starting_equity = (
            cfg.get("starting_equity")
            if "starting_equity" in cfg
            else cfg.get("startingEquity")
        )
        active_raw = cfg.get("active")
        active = True if active_raw is None else bool(active_raw)
        resolved_account_id = str(account_id).strip() if account_id else None
        resolved_account_id = _canonicalize_account_id(source, resolved_account_id)
        accounts[name] = AccountConfig(
            name=name,
            source=source,
            account_id=resolved_account_id,
            data_dir=Path(data_dir),
            funding_baseline=str(funding_baseline) if funding_baseline else None,
            exchange=str(exchange) if exchange else None,
            base_currency=str(base_currency) if base_currency else None,
            starting_equity=_parse_optional_float(starting_equity),
            active=active,
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
        resolved_account_id = account.account_id or account.name
        resolved_account_id = _canonicalize_account_id(account.source, resolved_account_id)
        return AccountContext(
            name=account.name,
            source=account.source,
            account_id=resolved_account_id,
            data_dir=account.data_dir,
            funding_baseline=account.funding_baseline,
            exchange=account.exchange or account.source,
            base_currency=account.base_currency,
            starting_equity=account.starting_equity,
            active=account.active,
        )
    data_dir = Path(env.get("TRADE_JOURNAL_DATA_DIR", "data"))
    source = str(env.get("TRADE_JOURNAL_SOURCE", "apex")).strip().lower() or "apex"
    account_id = env.get("TRADE_JOURNAL_ACCOUNT_ID")
    resolved_account_id = _canonicalize_account_id(source, account_id if account_id else resolved_name)
    return AccountContext(
        name=resolved_name,
        source=source,
        account_id=resolved_account_id,
        data_dir=data_dir,
        funding_baseline=None,
        exchange=source,
        base_currency=None,
        starting_equity=None,
        active=True,
    )


def resolve_data_path(
    override: str | None, context: AccountContext, filename: str
) -> Path:
    if override:
        return Path(override)
    return context.data_dir / filename


def _parse_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        value = stripped
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonicalize_account_id(source: str, account_id: str | None) -> str | None:
    if account_id is None:
        return None
    if source.strip().lower() != "hyperliquid":
        return account_id
    value = account_id.strip().lower()
    if not value:
        raise ValueError("Hyperliquid account_id is required (wallet address).")
    if not _looks_like_evm_address(value):
        raise ValueError(f"Invalid Hyperliquid wallet address: {account_id!r}")
    return value


def _looks_like_evm_address(value: str) -> bool:
    if not value.startswith("0x") or len(value) != 42:
        return False
    hexdigits = set("0123456789abcdef")
    return all(ch in hexdigits for ch in value[2:])
