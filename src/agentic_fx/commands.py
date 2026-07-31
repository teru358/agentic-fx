"""操作コマンド定義 — main.py シェルと (Phase 2) client.py で共有 (設計書 §8)。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals, missions, orders
from agentic_fx.store.approvals import AlreadyDecidedError
from agentic_fx.store.state import StateStore

_HELP = """コマンド一覧:
  status                     残高・モード・kill switch・直近 mission
  log [n]                    技術ログの直近 n 行 (default 20)
  activity [n] [カテゴリ]     activity ログ (NEWS/TECH/AGGREGATE/TRADE/IMPROVE/APPROVAL/SYSTEM)
  ask <質問>                  臨時 Mission (回答専用 — 発注はしない)
  approve <id> / reject <id> [理由]   承認操作
  killswitch reset           kill switch ラッチの解除 (人間の明示操作)
  stop                       graceful shutdown (シェルのみ)
(Phase 2 で追加: policy add / improve / news / model / mode / autopilot)"""


class Commands:
    def __init__(self, *, conn: sqlite3.Connection, state_store: StateStore,
                 broker: PaperBroker, trade_loop, activity: ActivityLog,
                 log_dir: Path, clock: Clock) -> None:
        self.conn = conn
        self.state = state_store
        self.broker = broker
        self.trade_loop = trade_loop
        self.activity = activity
        self.log_dir = log_dir
        self.clock = clock

    def dispatch(self, line: str) -> str:
        parts = line.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "status":
                return self._status()
            if cmd == "log":
                return self._log(int(args[0]) if args else 20)
            if cmd == "activity":
                n = int(args[0]) if args else 20
                cat = Category(args[1].upper()) if len(args) > 1 else None
                return "\n".join(self.activity.tail(n, cat)) or "(なし)"
            if cmd == "ask":
                if not args:
                    return "usage: ask <質問>"
                return self.trade_loop.ask_once(" ".join(args))
            if cmd == "approve" and args:
                approvals.decide(self.conn, int(args[0]), status="approved",
                                 decided_by="shell", now=self.clock.now())
                self.activity.write(Category.APPROVAL, "approved",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} approved"
            if cmd == "reject" and args:
                reason = " ".join(args[1:]) or None
                approvals.decide(self.conn, int(args[0]), status="rejected",
                                 decided_by="shell", now=self.clock.now(),
                                 reason=reason)
                self.activity.write(Category.APPROVAL, "rejected",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} rejected"
            if cmd == "killswitch" and args and args[0] == "reset":
                self.state.update(kill_switch_latched=False)
                self.activity.write(Category.SYSTEM, "kill_switch_reset",
                                    "human explicit reset via shell")
                return "kill switch ラッチを解除しました"
        except AlreadyDecidedError:
            return "その approval は決定済みです"
        except (ValueError, KeyError) as e:
            return f"エラー: {safe_error_text(e)}\n{_HELP}"
        except Exception as e:
            return f"エラー: {safe_error_text(e)}"
        return _HELP

    def _status(self) -> str:
        s = self.state.load()
        balance, equity = self.broker.equity()
        active = orders.list_by_status(self.conn, "open", "pending_fill",
                                       "protection_pending")
        recent = missions.recent(self.conn, 1)
        last = (f"{recent[0]['loop']}:{recent[0]['status']} "
                f"({recent[0]['started_at']})") if recent else "なし"
        return (f"mode: {s.mode.value} / autopilot: "
                f"{'on' if s.autopilot else 'off'} / kill switch: "
                f"{'LATCHED' if s.kill_switch_latched else 'ok'}\n"
                f"残高: {balance:,.0f} / エクイティ: {equity:,.0f}\n"
                f"アクティブ orders: {len(active)}\n"
                f"直近 mission: {last}")

    def _log(self, n: int) -> str:
        path = self.log_dir / "agentic.log"
        if not path.exists():
            return "(ログなし)"
        return "\n".join(
            path.read_text(encoding="utf-8").splitlines()[-n:])
