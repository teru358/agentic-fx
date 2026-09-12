"""操作コマンド定義 — main.py シェルと (Phase 2) client.py で共有 (設計書 §8)。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import (approvals, backlog, candidate_archives,
                              missions, orders, reflection_attempts,
                              reflections)
from agentic_fx.store.approvals import AlreadyDecidedError, ApprovalNotFoundError
from agentic_fx.store.state import StateStore

_HELP = """コマンド一覧:
  status                     残高・モード・kill switch・直近 mission
  log [n]                    技術ログの直近 n 行 (default 20)
  activity [n] [カテゴリ]     activity ログ (NEWS/TECH/AGGREGATE/TRADE/IMPROVE/APPROVAL/SYSTEM)
  ask <質問>                  臨時 Mission (回答専用 — 発注はしない)
  approve <id> / reject <id> [理由]   承認操作
  approval <id>               承認申請の詳細 (in_sample/holdout 成績を含む)
  approval retry <id>        承認手順を頭から再試行 (§5.3 契機③)
  killswitch reset           kill switch ラッチの解除 (人間の明示操作)
  reflect retry <order_id>   abandon された reflection を再試行対象へ戻す
  improve                    手動 improve one-shot (全バックログ担当)
  improve add <idea text>    バックログへ課題を追加
  backlog reject <id> / reopen <id> / note <id>   バックログの手動操作
  policy add <text>          policy/directives.md へ追記
  stop                       graceful shutdown (シェルのみ)
(Phase 2 で追加: news / model / mode / autopilot)"""


class Commands:
    def __init__(self, *, conn: sqlite3.Connection, state_store: StateStore,
                 broker: PaperBroker, trade_loop, activity: ActivityLog,
                 log_dir: Path, clock: Clock,
                 health_latch: HealthLatch | None = None,
                 improve_supervisor: object | None = None,
                 policy_path: Path | None = None,
                 plugins_root: Path | None = None,
                 settings: object | None = None) -> None:
        self.conn = conn
        self.state = state_store
        self.broker = broker
        self.trade_loop = trade_loop
        self.activity = activity
        self.log_dir = log_dir
        self.clock = clock
        self.health_latch = health_latch or HealthLatch()
        self.improve_supervisor = improve_supervisor
        self._policy_path = policy_path
        # プラン10 Task11e/11g: `approval retry <id>` シェルコマンド、および
        # kind="plugin" の approve/reject を switch.py (plugin flock 経由)
        # へ振り分けるために必要 (B-1 是正)。
        self.plugins_root = plugins_root
        self.settings = settings

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
                approval_id = int(args[0])
                # 裁定1 (11g Step1b): decide() 全廃。kind="plugin" は
                # plugin flock を経由する switch.approve_candidate を、
                # それ以外は apply_decision を直接通す (二重経路にしない)。
                row = self.conn.execute(
                    "SELECT kind FROM approval_requests WHERE id=?",
                    (approval_id,)).fetchone()
                if row is not None and row["kind"] == "plugin":
                    if self.plugins_root is None or self.settings is None:
                        return ("plugin approval backend "
                               "(plugins_root/settings) が未配線です")
                    from agentic_fx.plugin import switch as plugin_switch
                    plugin_switch.approve_candidate(
                        self.conn, approval_id, decided_by="shell",
                        now=self.clock.now(), plugins_root=self.plugins_root,
                        settings=self.settings, activity=self.activity)
                    # 検収 m5 是正: `approve_candidate` は正常な主要経路として
                    # non-pending 以外にも pending 留置で return しうる
                    # (§5.1: legacy_plain_present / candidate_missing /
                    # hash 不一致 / 未完ジャーナル)。旧稿は結果を確認せず
                    # 無条件に「approved」と報告していた — 実際の到達状態を
                    # 読み直して報告する。
                    outcome = self.conn.execute(
                        "SELECT status, reason FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()
                    if outcome is None or outcome["status"] != "approved":
                        status = outcome["status"] if outcome else "不明"
                        reason = (outcome["reason"] if outcome else None) or "-"
                        return (f"approval #{args[0]} は approved になりません"
                               f"でした (status={status}, reason={reason})")
                else:
                    approvals.apply_decision(
                        self.conn, approval_id, "approved", decided_by="shell",
                        now=self.clock.now(), commit=True)
                self.activity.write(Category.APPROVAL, "approved",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} approved"
            if cmd == "reject" and args:
                approval_id = int(args[0])
                reason = " ".join(args[1:]) or None
                row = self.conn.execute(
                    "SELECT kind FROM approval_requests WHERE id=?",
                    (approval_id,)).fetchone()
                if row is not None and row["kind"] == "plugin":
                    if self.plugins_root is None:
                        return "plugin approval backend (plugins_root) が未配線です"
                    from agentic_fx.plugin import switch as plugin_switch
                    plugin_switch.reject_candidate(
                        self.conn, approval_id, decided_by="shell",
                        reason=reason or "", now=self.clock.now(),
                        plugins_root=self.plugins_root, activity=self.activity)
                else:
                    approvals.apply_decision(
                        self.conn, approval_id, "rejected", decided_by="shell",
                        now=self.clock.now(), reason=reason, commit=True)
                self.activity.write(Category.APPROVAL, "rejected",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} rejected"
            if cmd == "approval" and len(args) == 2 and args[0] == "retry":
                # §5.3 契機③ (11e 新規命名): 手順を頭から再試行する。
                # plugin flock を経由する switch.retry_approval を呼ぶため
                # plugins_root/settings の配線が必須 (B-1 是正)。
                if self.plugins_root is None or self.settings is None:
                    return "approval retry backend (plugins_root/settings) が未配線です"
                from agentic_fx.plugin import switch as plugin_switch
                approval_id = int(args[1])
                plugin_switch.retry_approval(
                    self.conn, approval_id, decided_by="shell", now=self.clock.now(),
                    plugins_root=self.plugins_root, settings=self.settings,
                    activity=self.activity)
                self.activity.write(Category.APPROVAL, "retry",
                                    f"#{approval_id} via shell", ref_id=str(approval_id))
                return f"approval #{approval_id} を再試行しました"
            if cmd == "approval" and len(args) == 1 and args[0].isdigit():
                # [approval-payload-missing-gate-metrics] 是正 (A4 10 回目
                # claude #69 観測 A、2026-09-11): approve/reject する前に
                # 人間が in-sample/holdout の成績を読める詳細表示。従来
                # `afx>` には approval の中身を見る手段が無く (splash の
                # 承認待ち件数のみ)、承認判断が agent の自己申告に偏って
                # いた欠陥の一部。
                return self._approval_detail(int(args[0]))
            if cmd == "killswitch" and args and args[0] == "reset":
                self.state.update(kill_switch_latched=False)
                self.activity.write(Category.SYSTEM, "kill_switch_reset",
                                    "human explicit reset via shell")
                return "kill switch ラッチを解除しました"
            if cmd == "reflect" and len(args) == 2 and args[0] == "retry":
                order_id = int(args[1])
                order = orders.get(self.conn, order_id)
                if order is None:
                    raise ValueError(f"order #{order_id} does not exist")
                # F3 (レビュー 1 周目 codex Minor-3): 存在チェックだけでは、
                # まだ open な order や既に reflection 済みの order にも
                # 「戻しました」という誤った成功メッセージを返してしまう
                # (どちらも run_pending の抽出対象外で、台帳を触っても
                # 無意味)。
                status = order["status"]
                if status != "closed":
                    raise ValueError(
                        f"order #{order_id} は closed ではありません "
                        f"(status={status})")
                if reflections.get(self.conn, order_id) is not None:
                    raise ValueError(
                        f"order #{order_id} は既に reflection 済みです")
                had_attempt = reflection_attempts.attempts_of(
                    self.conn, order_id) > 0
                reflection_attempts.clear(self.conn, order_id)
                self.activity.write(Category.SYSTEM, "reflection_requeued",
                                    f"order_id={order_id} via shell",
                                    ref_id=str(order_id))
                suffix = "" if had_attempt else " (台帳に試行記録なし)"
                return (f"order #{order_id} を reflection 再試行対象へ"
                       f"戻しました{suffix}")
            if cmd == "improve" and not args:
                if self.improve_supervisor is None:
                    return "improve backend が未配線です"
                mission_id = self.improve_supervisor.submit_manual()
                return f"improve mission #{mission_id} を起動しました"
            if cmd == "improve" and args and args[0] == "add":
                text = " ".join(args[1:])
                if not text:
                    return "usage: improve add <idea text>"
                bid = backlog.add(self.conn, idea=text, source="user",
                                  now=self.clock.now())
                self.activity.write(Category.IMPROVE, "backlog_added",
                                    f"#{bid} via shell", ref_id=str(bid))
                return f"backlog #{bid} を追加しました"
            if cmd == "backlog" and len(args) == 2 and args[0] == "reject":
                bid = int(args[1])
                # 検収 B3 (2026-08-22): 設計書 §4.3 の状態機械 — reject は
                # open|observation からのみ。`backlog.set_status` (Task 8)
                # は `apply_approval_outcome` 用の汎用 setter でガードを
                # 持たないため、人間操作の入口である commands.py 側で
                # fail closed に検査する (現在の status も併せて未存在も
                # 検出 — M-e 対応)。
                row = self.conn.execute(
                    "SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
                if row is None:
                    return f"backlog #{bid} は存在しません"
                current_status = row["status"]
                if current_status not in ("open", "observation"):
                    return (f"backlog #{bid} は status={current_status} のため"
                            f" reject できません (open|observation からのみ可)")
                backlog.set_status(self.conn, bid, "rejected", self.clock.now(),
                                   last_result="human_rejected", commit=True)
                self.activity.write(Category.IMPROVE, "backlog_rejected",
                                    f"#{bid} via shell", ref_id=str(bid))
                return f"backlog #{bid} を rejected にしました"
            if cmd == "backlog" and len(args) == 2 and args[0] == "note":
                bid = int(args[1])
                row = self.conn.execute(
                    "SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
                if row is None:
                    return f"backlog #{bid} は存在しません"
                current_status = row["status"]
                if current_status not in ("open", "observation"):
                    return (f"backlog #{bid} は status={current_status} のため"
                            f" note にできません (open|observation からのみ可)")
                backlog.set_status(self.conn, bid, "note", self.clock.now(),
                                   last_result="human_noted", commit=True)
                self.activity.write(Category.IMPROVE, "backlog_noted",
                                    f"#{bid} via shell", ref_id=str(bid))
                return f"backlog #{bid} を note にしました"
            if cmd == "backlog" and len(args) == 2 and args[0] == "reopen":
                bid = int(args[1])
                # 検収 B3: reopen は done|rejected|note (終端) からのみ。
                # note からの reopen は d894983 (CR8) で追加 (note に出口を作る)。
                # `selected` (Mission 実行中) から reopen を許すと
                # 別 Mission の select_for_mission CAS が成功しうる
                # (設計書 §4 が挙げる「二重承認申請」への到達経路)。
                row = self.conn.execute(
                    "SELECT status FROM improvement_backlog WHERE id=?",
                    (bid,)).fetchone()
                if row is None:
                    return f"backlog #{bid} は存在しません"
                current_status = row["status"]
                if current_status not in ("done", "rejected", "note"):
                    return (f"backlog #{bid} は status={current_status} のため"
                            f" reopen できません (done|rejected|note からのみ可)")
                backlog.set_status(self.conn, bid, "open", self.clock.now(),
                                   last_result=("human_reopened" if current_status == "note"
                                                else "reopened"), commit=True)
                self.activity.write(Category.IMPROVE, "backlog_reopened",
                                    f"#{bid} via shell", ref_id=str(bid))
                return f"backlog #{bid} を open に戻しました"
            if cmd == "policy" and args and args[0] == "add":
                text = " ".join(args[1:])
                if not text:
                    return "usage: policy add <text>"
                if self._policy_path is None:
                    return "policy directives の path が未配線です"
                self._policy_path.parent.mkdir(parents=True, exist_ok=True)
                with self._policy_path.open("a", encoding="utf-8") as f:
                    f.write(f"\n- {text}\n")
                self.activity.write(Category.SYSTEM, "policy_added",
                                    text[:200])
                return "policy に追記しました"
        except ApprovalNotFoundError:
            # E1 裁定 (2026-08-25): 「ID 不存在」は「CAS 失敗 (決定済み)」と
            # 文言を分ける。既存の `test_approve_nonexistent` が pin して
            # いた「決定済み」という文言は、E1 是正前の apply_decision が
            # 両ケースを同一例外に混同していた頃の副産物であり、そのまま
            # 残すと「存在しない approval を決定済み扱いにした」という
            # E1 が正そうとしている混同そのものを再生産してしまう。
            # 確定-8 と同じ「欠陥の是正」として既存テストのアサーションも
            # 書き換える (最終報告の逸脱に明記)。
            return f"approval #{approval_id} は存在しません"
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
        result = (f"mode: {s.mode.value} / autopilot: "
                f"{'on' if s.autopilot else 'off'} / kill switch: "
                f"{'LATCHED' if s.kill_switch_latched else 'ok'}\n"
                f"残高: {balance:,.0f} / エクイティ: {equity:,.0f}\n"
                f"アクティブ orders: {len(active)}\n"
                f"直近 mission: {last}")
        if self.health_latch.is_latched():
            reasons = "; ".join(self.health_latch.summary()[:3])
            result += f"\nhealth: LATCHED ({reasons})"
        return result

    @staticmethod
    def _metrics_line(prefix: str, metrics: dict | None) -> str:
        """1 行分の pf/trades/avg_r/max_drawdown 表示。値が無ければ `-`。"""
        if metrics is None:
            return f"{prefix}: -"

        def _fmt(v):
            return "-" if v is None else v
        return (f"{prefix}: pf={_fmt(metrics.get('pf'))} "
                f"trades={_fmt(metrics.get('trades'))} "
                f"avg_r={_fmt(metrics.get('avg_r'))} "
                f"max_drawdown={_fmt(metrics.get('max_drawdown'))}")

    @classmethod
    def _metrics_lines(cls, prefix: str, value) -> list[str]:
        """`value` は単一 pair の metrics dict (`"trades"` キーを持つ) か、
        複数 pair の `{pair: metrics}` dict、あるいは None
        ([approval-payload-missing-gate-metrics] 是正前の旧 payload との
        後方互換)。単一 dict は 1 行、per-pair dict は pair ごとに 1 行。"""
        if value is None:
            return [cls._metrics_line(prefix, None)]
        if isinstance(value, dict) and "trades" in value:
            return [cls._metrics_line(prefix, value)]
        if isinstance(value, dict):
            return [cls._metrics_line(f"{prefix} {pair}", metrics)
                   for pair, metrics in value.items()]
        return [cls._metrics_line(prefix, None)]

    def _approval_detail(self, approval_id: int) -> str:
        row = self.conn.execute(
            "SELECT kind, status, payload_json FROM approval_requests "
            "WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return f"approval #{approval_id} は存在しません"
        try:
            payload = (json.loads(row["payload_json"])
                      if row["payload_json"] else {})
        except (TypeError, ValueError):
            payload = {}
        lines = [
            f"approval #{approval_id} kind={row['kind']} status={row['status']}",
            f"name={payload.get('name', '-')} "
            f"content_hash={payload.get('content_hash', '-')} "
            f"eval_timeframe={payload.get('eval_timeframe') or '-'}",
        ]
        lines += self._metrics_lines("in_sample", payload.get("in_sample"))
        lines += self._metrics_lines("holdout", payload.get("holdout"))
        lines.append(self._archive_line(payload))
        return "\n".join(lines)

    def _archive_line(self, payload: dict) -> str:
        """approval-quality 設計書 §C ([archive-artifact-hash-vs-
        submitted]): archive の引き当ては payload の `mission_id`/
        `content_hash` で行う — payload の `artifact_hash` は self-test
        修正 (提出直前の `test_plugin.py` 書き換え) でずれうるため引き
        当てキーには使わない (run12 観測 C、docstring は
        `store/candidate_archives.py::find_by_mission_content` 側にも
        明記)。見つからない (GC 済み・失敗終端等) ときは「archive 不明」
        と明示する。"""
        mission_id = payload.get("mission_id")
        content_hash = payload.get("content_hash")
        if mission_id is None or content_hash is None:
            return "archive=不明 (mission_id/content_hash 欠落)"
        row = candidate_archives.find_by_mission_content(
            self.conn, mission_id=mission_id, content_hash=content_hash)
        if row is None:
            return "archive=不明 (GC 済み・失敗終端等)"
        archive_path = row.get("archive_path")
        if archive_path is None:
            return "archive=不明 (パス欠落、GC 済みの可能性)"
        return f"archive={archive_path}"

    def _log(self, n: int) -> str:
        if n <= 0:
            return ""
        path = self.log_dir / "agentic.log"
        if not path.exists():
            return "(ログなし)"
        return "\n".join(
            path.read_text(encoding="utf-8").splitlines()[-n:])
