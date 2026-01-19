from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TABLES = [
    "fills",
    "orders",
    "funding",
    "liquidations",
    "historical_pnl",
    "sync_state",
    "schema_version",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export SQLite tables to JSON for backup.")
    parser.add_argument("--db", type=Path, default=Path("data/trade_journal.sqlite"), help="SQLite DB path.")
    parser.add_argument("--out-dir", type=Path, default=Path("data/exports"), help="Output directory.")
    parser.add_argument(
        "--tables",
        type=str,
        default="",
        help="Comma-separated list of tables to export (default: all).",
    )
    args = parser.parse_args(argv)

    tables = _parse_tables(args.tables)
    if not tables:
        tables = DEFAULT_TABLES

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(args.db),
        "tables": [],
    }

    for table in tables:
        rows = _fetch_rows(conn, table)
        if rows is None:
            continue
        manifest["tables"].append({"table": table, "rows": len(rows)})
        out_path = args.out_dir / f"{table}.json"
        out_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"exported_tables {len(manifest['tables'])}")
    return 0


def _parse_tables(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _fetch_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]] | None:
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.Error:
        return None
    output: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        for key in ("raw_json",):
            if key in data and isinstance(data[key], str):
                data[key] = _maybe_json(data[key])
        output.append(data)
    return output


def _maybe_json(value: str) -> Any:
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


if __name__ == "__main__":
    raise SystemExit(main())
