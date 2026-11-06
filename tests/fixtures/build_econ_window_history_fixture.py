"""経済指標回避帯の履歴 snapshot を本番 DB から抽出する (列 allowlist 方式)。

本番 DB は read-only で開く。保持するのは ALLOWED_COLUMNS の列だけで、
payload / output / transcript 列と他の表は落とす。

    uv run python tests/fixtures/build_econ_window_history_fixture.py \
        --db data/agentic.db --out tests/fixtures/econ_window_history_20261016_1031.sqlite
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

WINDOW_START = "2026-10-16T00:00:00+00:00"
WINDOW_END = "2026-10-31T00:00:00+00:00"

ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
    "missions": ("id", "loop", "trigger", "status", "started_at"),
    "trade_intents": ("id", "mission_id", "action"),
    "signals": ("id", "status", "kind", "claimed_by_mission_id"),
}

SNAPSHOT_DDL = """
CREATE TABLE missions (
  id INTEGER PRIMARY KEY,
  loop TEXT NOT NULL,
  trigger TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL
);
CREATE TABLE trade_intents (
  id INTEGER PRIMARY KEY,
  mission_id INTEGER NOT NULL REFERENCES missions(id),
  action TEXT
);
CREATE TABLE signals (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  kind TEXT NOT NULL,
  claimed_by_mission_id INTEGER REFERENCES missions(id)
);
"""

_MISSION_FILTER = "started_at >= :start AND started_at < :end"


def _select(table: str, where: str) -> str:
    cols = ", ".join(ALLOWED_COLUMNS[table])
    return f"SELECT {cols} FROM {table} WHERE {where} ORDER BY id"


def _insert(table: str) -> str:
    cols = ALLOWED_COLUMNS[table]
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})"
    )


def build(db_path: Path, out_path: Path) -> dict[str, int]:
    """db_path を read-only で開き、窓内の行を allowlist 列だけ out_path に写す。"""
    if out_path.exists():
        raise FileExistsError(f"{out_path} already exists")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        params = {"start": WINDOW_START, "end": WINDOW_END}
        inwin = f"mission_id IN (SELECT id FROM missions WHERE {_MISSION_FILTER})"
        sigwin = (
            "claimed_by_mission_id IN "
            f"(SELECT id FROM missions WHERE {_MISSION_FILTER})"
        )
        rows = {
            "missions": src.execute(
                _select("missions", _MISSION_FILTER), params
            ).fetchall(),
            "trade_intents": src.execute(
                _select("trade_intents", inwin), params
            ).fetchall(),
            "signals": src.execute(_select("signals", sigwin), params).fetchall(),
        }
    finally:
        src.close()
    dst = sqlite3.connect(out_path)
    try:
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.executescript(SNAPSHOT_DDL)
        for table, data in rows.items():
            dst.executemany(_insert(table), data)
        dst.commit()
    finally:
        dst.close()
    return {table: len(data) for table, data in rows.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    for table, n in build(args.db, args.out).items():
        print(f"{table}: {n}")


if __name__ == "__main__":
    main()
