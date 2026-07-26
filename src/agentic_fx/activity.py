"""activity ログ (カテゴリ軸)。ユーザーが読む行動記録 — 設計書 §13。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class Category(StrEnum):
    NEWS = "NEWS"
    TECH = "TECH"
    AGGREGATE = "AGGREGATE"
    TRADE = "TRADE"
    IMPROVE = "IMPROVE"
    APPROVAL = "APPROVAL"
    SYSTEM = "SYSTEM"


class ActivityLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, category: Category, event: str, summary: str,
              ref_id: str | None = None) -> None:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        clean = " ".join(summary.split())
        line = "\t".join([ts, category.value, event, clean, ref_id or "-"])
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def tail(self, n: int = 20, category: Category | None = None) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if category is not None:
            lines = [l for l in lines
                     if l.split("\t", 2)[1:2] == [category.value]]
        return lines[-n:]
