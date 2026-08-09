"""activity ログ (カテゴリ軸)。ユーザーが読む行動記録 — 設計書 §13。"""
from __future__ import annotations

import logging
from collections.abc import Callable
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
    def __init__(self, path: Path, *,
                 on_write_failure: Callable[[Exception], None] | None = None) -> None:
        self._path = path
        self._on_write_failure = on_write_failure
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

        I3 (fix round 1): 整形 3 行 (ts / clean / line の組み立て) も
        try の中に含める。以前は try の外にあり、``summary=None``
        (``None.split()``) や ``category`` が ``Category`` ではなく素の
        str (``str.value`` は存在しない) のケースで「決して送出しない」
        契約に反して例外が伝播していた (レビュアー実測)。
        """
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            clean = " ".join(summary.split())
            cat_value = category.value
            line = "\t".join([ts, cat_value, event, clean, ref_id or "-"])
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 — write は送出しない契約
            # category が Category でない場合に備え、ログ用表示は
            # `.value` に依存しない (getattr で素通しできればそれを使う)。
            cat_repr = getattr(category, "value", category)
            _log.warning("activity write failed (%s/%s): %s",
                         cat_repr, event, safe_error_text(e))
            if self._on_write_failure is not None:
                try:
                    self._on_write_failure(e)
                except Exception:  # noqa: BLE001 — write remains non-throwing
                    _log.exception("activity write failure callback failed")

    def tail(self, n: int = 20, category: Category | None = None) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if category is not None:
            lines = [l for l in lines
                     if l.split("\t", 2)[1:2] == [category.value]]
        return lines[-n:]
