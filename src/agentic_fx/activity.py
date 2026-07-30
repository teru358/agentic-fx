"""activity ログ (カテゴリ軸)。ユーザーが読む行動記録 — 設計書 §13。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from agentic_fx._safe_error import safe_error_text

_log = logging.getLogger("agentic_fx.activity")


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
        """activity 行を追記する。

        契約 (N2 再レビューで確定): **write は例外を送出しない**。activity
        は可観測性の記録であり、資金保護経路 (scheduler の隔離ハンドラ群 —
        mark_to_market_error / limit_fill_error / exit_check_error 等) の
        あらゆる場所から呼ばれるため、呼び出し側で毎回 try に包んで防ぐ
        方針は site 単位のモグラ叩きになる (N1 で 1 箇所塞いだ直後に別の
        呼び出し site で同型の欠陥 N2 が実測された)。発生源であるここで
        一度だけ塞ぐ — 書き込みに失敗しても記録漏れとして技術ログに
        warning を残すのみで、呼び出し元には決して伝播させない。
        """
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        clean = " ".join(summary.split())
        line = "\t".join([ts, category.value, event, clean, ref_id or "-"])
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 — write は送出しない契約
            _log.warning("activity write failed (%s/%s): %s",
                         category.value, event, safe_error_text(e))

    def tail(self, n: int = 20, category: Category | None = None) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if category is not None:
            lines = [l for l in lines
                     if l.split("\t", 2)[1:2] == [category.value]]
        return lines[-n:]
