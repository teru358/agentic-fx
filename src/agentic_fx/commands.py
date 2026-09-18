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
            "SELECT kind, status, payload_json, reason, decided_by, "
            "decided_at FROM approval_requests WHERE id=?",
            (approval_id,)).fetchone()
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
        # [reject-reason-leak] T0 (2026-09-12): 人間の却下理由の唯一の
        # 読み出し導線。`approval_requests.reason` は backlog の
        # `last_result` (固定文言 `rejected_by_human`) へは流れないため、
        # ここで表示しないと write-only になる。
        lines.append(
            f"reason={row['reason'] or '-'} "
            f"decided_by={row['decided_by'] or '-'} "
            f"decided_at={row['decided_at'] or '-'}")
        # [profitability-floor] T0 (2026-09-12、T1 Step 1-8 と対): 収益性
        # フロア警告 (`bless_candidate` の `floor_mode="warn"` 経路) の
        # payload キーを表示する。T1 実装前は payload に無いため fail-soft
        # (キー欠落時は行を出さない)。
        floor_warning = payload.get("floor_warning")
        if floor_warning:
            lines.append(f"floor_warning={floor_warning}")
        floor_detail = payload.get("floor_detail")
        if floor_detail:
            lines.append(f"floor_detail={floor_detail}")
        profitability_floor = payload.get("profitability_floor")
        if profitability_floor:
            lines.append(f"profitability_floor={profitability_floor}")
        # [indicator-consumption-wiring] §2.7 (codex r4 M1): indicator の
        # 承認詳細に依存 strategy を 2 欄で列挙する。**payload には入れない**
        # (表示時に `InventoryBuildResult` を逆引きする — payload は承認時点の
        # snapshot であり、承認待ちの間に依存関係が変わるため)。
        # (i) この候補の hash に pin 済み = `inventory.metas` のうち当該
        #     alias の pin が候補 `content_hash` と一致する strategy
        # (ii) 同名 indicator の別 hash に pin (承認すると外れる) =
        #     `phase1_metas` のうち同名 indicator への pin が候補 hash と不一致
        if payload.get("kind") == "indicator":
            here, elsewhere = self._dependent_strategies(
                indicator_name=payload.get("name"),
                candidate_hash=payload.get("content_hash"))
            lines.append(f"dependent_pinned_here={', '.join(here) or '-'}")
            lines.append(
                f"dependent_pinned_elsewhere={', '.join(elsewhere) or '-'}")
        lines.append(self._archive_line(payload))
        return "\n".join(lines)

    def _dependent_strategies(self, *, indicator_name, candidate_hash
                              ) -> "tuple[list[str], list[str]]":
        """`_approval_detail` の 2 欄を作る。**決定順 (最新承認の approval
        `id` の昇順)** で並べる。

        `approval_requests` に `name` 列は無いので、
        `json_extract(payload_json,'$.name')` で引く。

        inventory 構築に失敗した場合は両方空 (表示は fail-soft)。
        `plugins_root` / `settings` は任意引数なので `None` があり得る —
        その場合も両方空を返す (fail-soft)。plugin の root は
        `self.plugins_root` (`self.root` は存在しない — opus r1 C4)。"""
        from agentic_fx.tools import plugin_loader as tools_plugin_loader
        if self.plugins_root is None or self.settings is None:
            return [], []
        try:
            result = tools_plugin_loader.approved_plugins(
                self.conn, self.plugins_root, settings=self.settings)
        except Exception:  # noqa: BLE001 — 表示は fail-soft
            return [], []

        def _pins_to(meta) -> list[str]:
            return [ref.pin for ref in meta.indicators
                    if ref.plugin == indicator_name and ref.pin is not None]

        # [codex plan r1 I9] 各 strategy 名の「最新承認 approval id」を
        # 1 クエリで引き、その昇順 = 決定順に並べる。承認行が無い名前
        # (まだ承認されていない配備物) は id を持たないので**末尾**に、
        # その中では名前昇順で安定させる。
        decided = {
            row["name"]: row["last_id"] for row in self.conn.execute(
                "SELECT json_extract(payload_json,'$.name') AS name, "
                "       MAX(id) AS last_id "
                "FROM approval_requests "
                "WHERE status='approved' "
                "  AND json_extract(payload_json,'$.kind')='strategy' "
                "GROUP BY name")
            if row["name"] is not None}

        def _in_decision_order(names: list[str]) -> list[str]:
            return sorted(names,
                          key=lambda n: (decided.get(n) is None,
                                         decided.get(n, 0), n))

        # /code-review 2 周目 CR4 是正 (2026-09-18、設計書 §2.7 v1.6):
        # (i) 欄も `phase1_metas` を走査する。旧実装は第 2 相 admit 済
        # (`result.inventory.metas` = live) だけを見ていたため、
        # 「候補 hash に pin 済だが現承認 hash と不一致なので live でない」
        # strategy = **まさにこの候補を承認すれば復帰する strategy** が
        # (i) にも (ii) にも出ず (その pin は candidate_hash と一致するので
        # (ii) の条件 `p != candidate_hash` も満たさない)、人間の承認判断
        # から完全に隠れていた。
        here = _in_decision_order(
            [m.name for m in result.phase1_metas
             if m.kind == "strategy" and candidate_hash in _pins_to(m)])
        elsewhere = _in_decision_order(
            [m.name for m in result.phase1_metas
             if m.kind == "strategy"
             and any(p != candidate_hash for p in _pins_to(m))])
        return here, elsewhere

    def _archive_line(self, payload: dict) -> str:
        """approval-quality 設計書 §C ([archive-artifact-hash-vs-
        submitted]): archive の引き当ては payload の `mission_id`/
        `content_hash` で行う — payload の `artifact_hash` は self-test
        修正 (提出直前の `test_plugin.py` 書き換え) でずれうるため引き
        当てキーには使わない (run12 観測 C、docstring は
        `store/candidate_archives.py::find_by_mission_content` 側にも
        明記)。見つからない (GC 済み・失敗終端等) ときは「archive 不明」
        と明示する。payload の型が壊れている (`mission_id` が int でない、
        `content_hash` が非空文字列でない) 場合も例外にせず「archive=
        不明」を返す (codex 1周目 I3 是正 — 手動修復・旧版移行・部分
        破損時に `_approval_detail` 全体が fail-hard するのを防ぐ)。"""
        mission_id = payload.get("mission_id")
        content_hash = payload.get("content_hash")
        if mission_id is None or content_hash is None:
            return "archive=不明 (mission_id/content_hash 欠落)"
        if (type(mission_id) is not int or type(content_hash) is not str
                or not content_hash):
            return "archive=不明 (mission_id/content_hash 不正)"
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
